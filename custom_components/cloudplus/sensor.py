"""Sensor platform for CloudEdge / Meari — battery, environment, baby status, PTZ."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .coordinator import CloudEdgeMeariCoordinator
from .runtime import add_camera_entities
from .entity import CloudEdgeMeariEntity, CloudEdgeMeariIotNumericEntity
from .coordinator.iot import is_area_value
from .meari_commands import (
    BABY_STATUS,
    DETECTION_AREAS,
    HUMIDITY,
    PTZ_INFO,
    TEMPERATURE,
    WIFI_STRENGTH,
)

# Only PTZ Position polls (the rest are pushed by the coordinator): the camera
# is asked for its live position this often.
SCAN_INTERVAL = timedelta(seconds=30)

# (min percent, icon) ladders, highest threshold first.
_CHARGING_BATTERY_ICONS = (
    (90, "mdi:battery-charging-100"),
    (70, "mdi:battery-charging-80"),
    (50, "mdi:battery-charging-60"),
    (30, "mdi:battery-charging-40"),
    (10, "mdi:battery-charging-20"),
)
_BATTERY_ICONS = (
    (95, "mdi:battery"),
    (85, "mdi:battery-90"),
    (75, "mdi:battery-80"),
    (65, "mdi:battery-70"),
    (55, "mdi:battery-60"),
    (45, "mdi:battery-50"),
    (35, "mdi:battery-40"),
    (25, "mdi:battery-30"),
    (15, "mdi:battery-20"),
    (5, "mdi:battery-10"),
)


@dataclass(frozen=True)
class IotSensorSpec:
    """Declarative spec for an IoT-backed sensor entity."""

    feature: str
    code: int
    name: str
    device_class: SensorDeviceClass
    unit: str
    icon: str | None = None
    # Raw-to-native divisor. The vendor scale is not symmetric between the two
    # sensors; per the Arenti 5.1.2 device-config parser:
    #
    #   "1008" -> optDouble() / 1000.0 -> setTemperature(float)  => milli-degC
    #   "1009" -> optInt()             -> setHumidity(int)       => whole %RH
    #
    # The threshold codes are milli-units on both axes (the alarm screen
    # divides 242/243 and 244/245 by 1000), so they are not a guide to the
    # current-value scale.
    divisor: float = 1.0
    # Vendor "no reading" sentinel, compared against the native (post-divisor)
    # value. The app derives haveSensor as NOT (temperature == 255.0 AND
    # humidity == 255); its temperature compare happens after the /1000, so
    # the sentinel is 255.0 in native units for both.
    sentinel: float | None = None
    precision: int | None = None
    # Explicit zero hides stale telemetry; absent flags retain legacy support.
    capability: str | None = None


IOT_SENSORS: tuple[IotSensorSpec, ...] = (
    IotSensorSpec(
        "temp_sensor",
        TEMPERATURE,
        "Temperature",
        SensorDeviceClass.TEMPERATURE,
        UnitOfTemperature.CELSIUS,
        divisor=1000.0,
        sentinel=255.0,  # raw 255000
        precision=1,
        capability="tmpr",
    ),
    IotSensorSpec(
        "humidity_sensor",
        HUMIDITY,
        "Humidity",
        SensorDeviceClass.HUMIDITY,
        PERCENTAGE,
        # No divisor: code 1009 is already whole percent (see IotSensorSpec).
        sentinel=255.0,  # raw 255
        precision=0,
        capability="hmd",
    ),
    IotSensorSpec(
        "wifi_signal",
        WIFI_STRENGTH,
        "WiFi Signal",
        None,
        PERCENTAGE,
        icon="mdi:wifi",
    ),
)


def _supports_sensor(coordinator: CloudEdgeMeariCoordinator, spec: IotSensorSpec) -> bool:
    advertised = coordinator.iot_capability(spec.capability)
    if advertised is not None:
        return advertised > 0
    return coordinator.supports_iot(spec.feature) or coordinator.has_iot_code(spec.code)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up CloudEdge / Meari sensors from a config entry."""
    add_camera_entities(hass, entry, async_add_entities, _camera_entities)


def _camera_entities(
    coord: CloudEdgeMeariCoordinator, entry: ConfigEntry
) -> list[SensorEntity]:
    entities: list[SensorEntity] = []
    if coord.is_battery_camera:
        entities.append(CloudEdgeMeariBatterySensor(coord, entry))
        entities.append(CloudEdgeMeariChargeStatusSensor(coord, entry))
    entities.extend(
        CloudEdgeMeariIotSensor(coord, entry, spec)
        for spec in IOT_SENSORS
        if _supports_sensor(coord, spec)
    )
    if coord.has_iot_code(BABY_STATUS):
        entities.append(CloudEdgeMeariBabyStatusSensor(coord, entry))
    if coord.has_ptz:
        entities.append(CloudEdgeMeariPtzPositionSensor(coord, entry))
    if any(is_area_value(coord.get_iot_value(code)) for code in DETECTION_AREAS.values()):
        entities.append(CloudEdgeMeariDetectionAreasSensor(coord, entry))
    return entities


def _parse_json(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


class CloudEdgeMeariPtzPositionSensor(CloudEdgeMeariEntity, SensorEntity):
    """Where the camera is pointing, as the camera reports it (IoT 1034).

    The value is shown as reported; a JSON object's fields are also
    attributes, for automations. Polled live from the camera, except on a
    battery camera that is asleep (asking would wake it).
    """

    _attr_name = "PTZ Position"
    _attr_icon = "mdi:axis-arrow"
    _attr_should_poll = True

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_ptz_position"

    async def async_update(self) -> None:
        coord = self._coordinator
        if coord.is_battery_camera and not coord.camera_awake:
            return
        await self.hass.async_add_executor_job(coord.refresh_ptz_position)

    @property
    def native_value(self) -> str | None:
        value = self._coordinator.get_iot_value(PTZ_INFO)
        if value is None:
            return None
        if not isinstance(value, str):
            value = json.dumps(value, separators=(",", ":"))
        return value[:255]

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        parsed = _parse_json(self._coordinator.get_iot_value(PTZ_INFO))
        return dict(parsed) if isinstance(parsed, dict) else {}


class CloudEdgeMeariDetectionAreasSensor(CloudEdgeMeariEntity, SensorEntity):
    """The camera's detection areas (IoT 177/232/247/303), read-only.

    State is how many area codes the camera reports; each one's value is an
    attribute, in the shape cloudplus.set_detection_area writes back.
    """

    _attr_name = "Detection Areas"
    _attr_icon = "mdi:vector-polygon"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_detection_areas"

    def _areas(self) -> dict[str, Any]:
        areas = {}
        for name, code in DETECTION_AREAS.items():
            value = self._coordinator.get_iot_value(code)
            if is_area_value(value):
                areas[name] = _parse_json(value)
        return areas

    @property
    def native_value(self) -> int:
        return len(self._areas())

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._areas()


_BABY_STATES = {0: "awake", 1: "crying", 2: "sleeping"}


class CloudEdgeMeariBabyStatusSensor(CloudEdgeMeariEntity, SensorEntity):
    """The camera's own awake / crying / sleeping estimate (IoT 863).

    Polled with the other camera settings (every few minutes): fine for
    "asleep", too slow for crying, which the Crying binary sensor covers.
    """

    _attr_name = "Baby Status"
    _attr_icon = "mdi:baby-face-outline"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(_BABY_STATES.values())

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_baby_status"

    @property
    def native_value(self) -> str | None:
        try:
            return _BABY_STATES.get(int(self._coordinator.get_iot_value(BABY_STATUS)))
        except (TypeError, ValueError):
            return None


class CloudEdgeMeariIotSensor(CloudEdgeMeariIotNumericEntity, SensorEntity):
    """Sensor entity backed by a Meari IoT value."""

    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        coordinator: CloudEdgeMeariCoordinator,
        entry: ConfigEntry,
        spec: IotSensorSpec,
    ) -> None:
        super().__init__(coordinator, entry, spec)
        self._attr_device_class = spec.device_class
        self._attr_native_unit_of_measurement = spec.unit
        self._attr_suggested_display_precision = spec.precision
        self._attr_unique_id = f"{coordinator.device_uuid}_iot_sensor_{spec.code}"


class CloudEdgeMeariChargeStatusSensor(CloudEdgeMeariEntity, SensorEntity):
    """Sensor for battery charge status."""

    _attr_name = "Charge Status"
    _attr_icon = "mdi:battery-charging"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_charge_status"

    @property
    def available(self) -> bool:
        return (
            self._coordinator.available
            and self._coordinator.battery_percent is not None
        )

    @property
    def native_value(self) -> str | None:
        if self._coordinator.battery_percent is None:
            return None
        return "Charging" if self._coordinator.battery_charging else "Not Charging"


class CloudEdgeMeariBatterySensor(CloudEdgeMeariEntity, SensorEntity):
    """Sensor for battery level."""

    _attr_name = "Battery"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_icon = "mdi:battery"

    def __init__(
        self, coordinator: CloudEdgeMeariCoordinator, entry: ConfigEntry
    ) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{coordinator.device_uuid}_battery"

    @property
    def native_value(self) -> int | None:
        return self._coordinator.battery_percent

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        attrs: dict[str, Any] = {}
        if self._coordinator.battery_charging:
            attrs["charging"] = True
        return attrs

    @property
    def icon(self) -> str:
        pct = self._coordinator.battery_percent
        charging = self._coordinator.battery_charging

        if pct is None:
            return "mdi:battery-unknown"

        if charging:
            for threshold, icon in _CHARGING_BATTERY_ICONS:
                if pct >= threshold:
                    return icon
            return "mdi:battery-charging-outline"

        for threshold, icon in _BATTERY_ICONS:
            if pct >= threshold:
                return icon
        return "mdi:battery-alert-variant-outline"
