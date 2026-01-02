# custom_components/orvibo_switch/fan.py
import logging
from typing import Optional
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.fan import FanEntity, FanEntityFeature
from homeassistant.core import HomeAssistant, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from .coordinator import OrviboSwitchCoordinator
from .functions import format_mac
from .hass import (
    get_room_name_by_room_id,
    get_model_name_by_model_id
)
from .const import(
    DOMAIN,
    MANUFACTURER,
    DEVICE_TYPE,
    ORVIBO_SWITCH_MODEL
)

_LOGGER = logging.getLogger(__name__)

async def async_setup_entry(hass: HomeAssistant,
                            entry: ConfigEntry,
                            async_add_entities: AddEntitiesCallback):
    """设置新风实体"""
    coordinator: OrviboSwitchCoordinator = hass.data[DOMAIN]["coordinator"]

    # 创建新风实体
    entities = []
    for device_id in coordinator.device_states:
        device_state = coordinator.device_states[device_id]
        model = device_state.get('model')
        device_type = ORVIBO_SWITCH_MODEL.get(model, "Switch")
        if device_type == "Ventilation":
            entities.append(WifiVentilationDevice(coordinator, device_id))

    async_add_entities(entities)
    _LOGGER.debug(f"添加了{len(entities)}个新风实体")

class WifiVentilationDevice(CoordinatorEntity, FanEntity):
    def __init__(self, coordinator: OrviboSwitchCoordinator, device_id):
        super().__init__(coordinator)

        device_state = coordinator.device_states[device_id]
        # 核心属性（依赖核心字段）
        self.device_id = device_id
        self._attr_unique_id = f"{DEVICE_TYPE}_fan_{device_id}"
        self._attr_name = f"{device_state.get('device_name')}"
        self._attr_entity_category = None
        self._attr_icon = "mdi:air-filter"

        room_id = device_state.get("room_id")
        model_id = device_state.get('model')
        online = device_state.get("online")
        device_uid = device_state.get("device_uid")
        # --------------- 额外字段的使用 ---------------
        # 1. 设备属性（HA 界面「属性」面板中显示）

        self._attr_extra_state_attributes = {
            "room_name": get_room_name_by_room_id(coordinator.hass, room_id) if room_id else "",
            "online_status": "在线" if online else "离线",
            "mac_address": format_mac(device_uid),
            "product_name": get_model_name_by_model_id(coordinator.hass, model_id) if model_id else "",
        }
        self._attr_device_info = {  # 绑定设备（关键，HA要求实体归属设备才易展示）
            "identifiers": {(f"{DEVICE_TYPE}_integration", f"device_{device_id}")},
            "name": f"{self._attr_name}",
            "model": f"{model_id}",
            "manufacturer": MANUFACTURER,
        }

        # 新风特有属性
        self._attr_supported_features = (
            FanEntityFeature.PRESET_MODE |
            FanEntityFeature.TURN_ON |
            FanEntityFeature.TURN_OFF
        )
        self._attr_preset_modes = ["停", "慢", "快"]
        self._attr_oscillating = False
        # 禁用百分比风速支持
        self._attr_percentage = None
        self._attr_percentage_step = None
        # 禁用旧的速度列表功能
        self._attr_speed_list = None

        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @property
    def available(self) -> bool:
        """返回设备是否可用（在线）"""
        if not self.coordinator.device_states:
            return False
        device_state = self.coordinator.device_states.get(self.device_id, {})
        if not device_state:
            return False
        
        # 根据用户反馈，online=1表示在线，0为离线
        return device_state.get('online', 1) != 0

    @property
    def is_on(self) -> bool:
        """返回设备是否开启"""
        if not self.coordinator.device_states:
            return False
        device_state = self.coordinator.device_states.get(self.device_id, {})
        return device_state.get("state", False)

    @property
    def speed(self) -> Optional[str]:
        """返回当前风速（保持兼容，实际使用preset_mode）"""
        return None

    @property
    def preset_mode(self) -> str:
        """返回当前预设模式"""
        if not self.coordinator.device_states:
            return "停"
        
        device_state = self.coordinator.device_states.get(self.device_id, {})
        
        # 获取已解析的风速档位
        fan_speed = device_state.get("fan_speed", "停")
        
        return fan_speed

    async def async_turn_on(self, speed: Optional[str] = None, percentage: Optional[int] = None, preset_mode: Optional[str] = None, **kwargs) -> None:
        """开启设备"""
        # 新风设备没有单独的开关指令，通过设置预设模式来控制开关
        if preset_mode:
            await self.async_set_preset_mode(preset_mode)
        elif speed:
            # 保持与旧的speed参数兼容
            await self.async_set_preset_mode(speed)
        else:
            # 默认开启慢档
            await self.async_set_preset_mode("慢")

    async def async_turn_off(self, **kwargs) -> None:
        """关闭设备"""
        # 新风设备没有单独的关闭指令，通过设置停止预设模式来关闭
        await self.async_set_preset_mode("停")

    async def async_set_speed(self, speed: str) -> None:
        """设置风速（保持兼容，实际使用preset_mode）"""
        await self.async_set_preset_mode(speed)

    async def async_set_preset_mode(self, preset_mode: str) -> None:
        """设置预设模式"""
        # 根据实际API实现预设模式控制
        _LOGGER.debug(f"设置新风{self.device_id}预设模式为{preset_mode}")
        # 预设模式映射：慢 -> value1=0，停 -> value1=50，快 -> value1=100
        if preset_mode == "慢":
            await self.coordinator.async_ventilation_state_update(self.device_id, 0)
        elif preset_mode == "停":
            await self.coordinator.async_ventilation_state_update(self.device_id, 50)
        elif preset_mode == "快":
            await self.coordinator.async_ventilation_state_update(self.device_id, 100)



    async def async_toggle(self, **kwargs) -> None:
        """切换设备开关状态"""
        if self.is_on:
            await self.async_turn_off()
        else:
            await self.async_turn_on()

    @callback
    def _handle_coordinator_update(self) -> None:
        """当协调器通知更新时刷新状态"""
        self.async_write_ha_state()

    @property
    def should_poll(self) -> bool:
        return False        # 禁用轮询，依赖Coordinator推送更新

    async def async_added_to_hass(self):
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))

    async def async_will_remove_from_hass(self):
        """实体移除时，停止Coordinator"""
        await self.coordinator.stop()
