# custom_components/orvibo_switch/climate.py
import logging
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.components.climate import ClimateEntity, HVACMode
from homeassistant.components.climate import ClimateEntityFeature
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

async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback):
    """设置空调实体"""
    coordinator: OrviboSwitchCoordinator = hass.data[DOMAIN]["coordinator"]
    
    _LOGGER.debug(f"开始设置空调实体，当前设备状态数量：{len(coordinator.device_states)}")
    
    # 打印所有设备状态，用于调试
    for device_id, device_state in coordinator.device_states.items():
        model = device_state.get('model')
        device_type = ORVIBO_SWITCH_MODEL.get(model, "Switch")
        online = device_state.get("online", 0)
        _LOGGER.debug(f"设备ID: {device_id}, 型号: {model}, 类型: {device_type}, 在线状态: {online}")

    # 创建空调实体
    entities = []
    for device_id in coordinator.device_states:
        device_state = coordinator.device_states[device_id]
        model = device_state.get('model')
        device_type = ORVIBO_SWITCH_MODEL.get(model, "Switch")
        if device_type == "Air Conditioner":
            _LOGGER.debug(f"创建空调实体，设备ID: {device_id}, 设备状态: {device_state}")
            entities.append(WifiAirConditionerDevice(coordinator, device_id))

    async_add_entities(entities)
    _LOGGER.debug(f"添加了{len(entities)}个空调实体")

class WifiAirConditionerDevice(CoordinatorEntity, ClimateEntity):
    def __init__(self, coordinator: OrviboSwitchCoordinator, device_id):
        super().__init__(coordinator)

        device_state = coordinator.device_states[device_id]
        # 核心属性（依赖核心字段）
        self.device_id = device_id
        self._attr_unique_id = f"{DEVICE_TYPE}_climate_{device_id}"
        self._attr_name = f"{device_state.get('device_name')}"
        self._attr_entity_category = None
        self._attr_icon = "mdi:air-conditioner"

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

        # 空调特有属性
        # 设置支持的HVAC模式
        self._attr_hvac_modes = [HVACMode.OFF, HVACMode.DRY, HVACMode.FAN_ONLY, HVACMode.COOL, HVACMode.HEAT]
        
        # 初始化当前HVAC模式为设备的实际状态
        is_on = device_state.get("state", False)
        value2 = device_state.get("value2", 3)  # 获取模式值，默认为制冷
        
        # 根据value2映射到HVAC模式
        if not is_on:
            self._attr_hvac_mode = HVACMode.OFF
        elif value2 == 2:
            self._attr_hvac_mode = HVACMode.DRY  # value2=2实际对应除湿模式
        elif value2 == 7:
            self._attr_hvac_mode = HVACMode.FAN_ONLY  # value2=7实际对应仅送风模式
        elif value2 == 3:
            self._attr_hvac_mode = HVACMode.COOL
        elif value2 == 4:
            self._attr_hvac_mode = HVACMode.HEAT
        else:
            self._attr_hvac_mode = HVACMode.OFF
        
        self._attr_supported_features = ClimateEntityFeature.TARGET_TEMPERATURE | ClimateEntityFeature.FAN_MODE
        self._attr_temperature_unit = "°C"
        self._attr_min_temp = 16
        self._attr_max_temp = 30
        self._attr_target_temperature_step = 1
        self._attr_fan_modes = ["低风", "中风", "高风"]
        
        # 初始化目标温度
        self._attr_target_temperature = device_state.get("target_temperature", 25)
        # 初始化当前温度
        self._attr_current_temperature = device_state.get("current_temperature", 25)
        # 初始化风速
        value3 = device_state.get("value3", 1)  # 获取风速值，默认为低风
        self._attr_fan_mode = self._attr_fan_modes[value3 - 1] if 1 <= value3 <= 3 else "低风"

        self.async_on_remove(
            self.coordinator.async_add_listener(self._handle_coordinator_update)
        )

    @property
    def available(self) -> bool:
        """返回设备是否可用（在线）"""
        if not self.coordinator.device_states:
            _LOGGER.debug(f"设备{self.device_id}不可用：协调器设备状态为空")
            return False
        device_state = self.coordinator.device_states.get(self.device_id, {})
        if not device_state:
            _LOGGER.debug(f"设备{self.device_id}不可用：找不到设备状态")
            return False
        
        # 根据用户反馈，online=1表示在线，0为离线
        available = device_state.get('online', 1) != 0
        _LOGGER.debug(f"设备{self.device_id}可用状态：{available}，online值：{device_state.get('online')}")
        return available

    @property
    def hvac_mode(self) -> str:
        """返回当前的HVAC模式"""
        if not self.coordinator.device_states:
            return HVACMode.OFF
        device_state = self.coordinator.device_states.get(self.device_id, {})
        is_on = device_state.get("state", False)
        value2 = device_state.get("value2", 3)  # 获取模式值，默认为制冷
        
        # 根据value2映射到HVAC模式
        if not is_on:
            mapped_mode = HVACMode.OFF
        elif value2 == 2:
            mapped_mode = HVACMode.DRY  # value2=2实际对应除湿模式
        elif value2 == 7:
            mapped_mode = HVACMode.FAN_ONLY  # value2=7实际对应仅送风模式
        elif value2 == 3:
            mapped_mode = HVACMode.COOL
        elif value2 == 4:
            mapped_mode = HVACMode.HEAT
        else:
            mapped_mode = HVACMode.OFF
            
        # 记录映射前后的模式值，方便调试模式映射问题
        # _LOGGER.info(f"设备{self.device_id}模式映射 - 原始value2: {value2}, 设备开关状态: {is_on}, 映射后模式: {mapped_mode}")
        # _LOGGER.debug(f"设备{self.device_id}完整状态: {device_state}")
        
        return mapped_mode

    @property
    def target_temperature(self) -> float:
        """返回目标温度"""
        # 从device_states获取，若不存在则使用默认值
        if not self.coordinator.device_states:
            return 25
        device_state = self.coordinator.device_states.get(self.device_id, {})
        return device_state.get("target_temperature", 25)

    @property
    def current_temperature(self) -> float:
        """返回当前温度"""
        # 从device_states获取，若不存在则使用默认值
        if not self.coordinator.device_states:
            return 25
        device_state = self.coordinator.device_states.get(self.device_id, {})
        return device_state.get("current_temperature", 25)

    @property
    def fan_mode(self) -> str:
        """返回当前风速"""
        # 从device_states获取，若不存在则使用默认值
        if not self.coordinator.device_states:
            return "低风"
        device_state = self.coordinator.device_states.get(self.device_id, {})
        value3 = device_state.get("value3", 1)  # 获取风速值，默认为低风
        
        # 根据value3映射到风速模式
        if value3 == 1:
            return "低风"
        elif value3 == 2:
            return "中风"
        elif value3 == 3:
            return "高风"
        else:
            return "低风"

    async def async_set_hvac_mode(self, hvac_mode: str) -> None:
        """设置HVAC模式"""
        _LOGGER.debug(f"设置空调{self.device_id}模式为{hvac_mode}")
        
        # 确保device_states存在
        if not self.coordinator.device_states:
            return
            
        # 获取当前设备状态
        device_state = self.coordinator.device_states.get(self.device_id, {})
        
        # 根据HVAC模式映射到value1和value2
        if hvac_mode == HVACMode.OFF:
            value1 = 1  # 1为关
            value2 = device_state.get("value2", 3)  # 保持当前模式
        else:
            value1 = 0  # 0为开
            if hvac_mode == HVACMode.DRY:
                value2 = 2  # 除湿模式对应value2=2
            elif hvac_mode == HVACMode.FAN_ONLY:
                value2 = 7  # 仅送风模式对应value2=7
            elif hvac_mode == HVACMode.COOL:
                value2 = 3
            elif hvac_mode == HVACMode.HEAT:
                value2 = 4
            else:
                value2 = 3  # 默认制冷
        
        # 获取其他当前参数
        value3 = device_state.get("value3", 1)  # 当前风速
        value4 = device_state.get("value4", 0)  # 当前温度值
        
        # 发送控制指令
        await self.coordinator.async_air_conditioner_state_update(self.device_id, value1, value2, value3, value4)

    async def async_set_temperature(self, **kwargs) -> None:
        """设置温度（Home Assistant调用的异步方法）"""
        _LOGGER.debug(f"调用async_set_temperature方法，参数: {kwargs}")
        # 将调用转发给async_set_target_temperature方法
        await self.async_set_target_temperature(**kwargs)

    def set_temperature(self, **kwargs) -> None:
        """设置温度（Home Assistant调用的同步方法）"""
        _LOGGER.debug(f"调用set_temperature方法，参数: {kwargs}")
        # 在同步方法中调用异步方法
        self.hass.async_add_job(self.async_set_temperature, **kwargs)
    
    async def async_set_target_temperature(self, **kwargs) -> None:
        """设置目标温度"""
        _LOGGER.debug(f"开始执行温度设置动作，参数: {kwargs}")
        
        # 从kwargs中获取温度值
        temperature = kwargs.get("temperature")
        if temperature is None:
            _LOGGER.error("无法设置温度：参数中没有temperature值")
            return
        
        _LOGGER.debug(f"设置空调{self.device_id}目标温度为{temperature}")
        
        try:
            # 确保device_states存在
            if not self.coordinator.device_states:
                _LOGGER.error("无法设置温度：协调器设备状态为空")
                return
                
            # 获取当前设备状态
            device_state = self.coordinator.device_states.get(self.device_id, {})
            
            # 获取当前参数
            value1 = device_state.get("value1", 0)  # 当前开关状态
            value2 = device_state.get("value2", 3)  # 当前模式
            value3 = device_state.get("value3", 1)  # 当前风速
            current_value4 = device_state.get("value4", 0)  # 当前温度值
            
            # 解析当前室内温度（低16位）
            indoor_temperature = (current_value4 & 0xFFFF) // 100
            
            # 确保设备处于开启状态
            if value1 == 1:
                _LOGGER.debug(f"设备{self.device_id}处于关闭状态，设置温度前将其开启")
                value1 = 0
                # 如果设备处于关闭状态，确保有默认的模式值
                value2 = device_state.get("value2", 3)  # 默认制冷模式
            
            # 计算新的value4
            target_temp_scaled = int(temperature * 100)
            indoor_temp_scaled = int(indoor_temperature * 100)
            new_value4 = (target_temp_scaled << 16) | indoor_temp_scaled
            
            _LOGGER.debug(f"发送温度控制指令 - 设备ID: {self.device_id}, value1: {value1}, value2: {value2}, value3: {value3}, new_value4: {new_value4}")
            
            # 发送控制指令
            result = await self.coordinator.async_air_conditioner_state_update(self.device_id, value1, value2, value3, new_value4)
            if result:
                _LOGGER.debug(f"温度控制指令发送成功")
            else:
                _LOGGER.error(f"温度控制指令发送失败")
        except Exception as e:
            _LOGGER.error(f"设置温度时发生异常: {str(e)}", exc_info=True)
            raise

    async def async_set_fan_mode(self, fan_mode: str) -> None:
        """设置风速"""
        _LOGGER.debug(f"设置空调{self.device_id}风速为{fan_mode}")
        
        # 确保device_states存在
        if not self.coordinator.device_states:
            return
            
        # 获取当前设备状态
        device_state = self.coordinator.device_states.get(self.device_id, {})
        
        # 根据风速模式映射到value3
        if fan_mode == "低风":
            value3 = 1
        elif fan_mode == "中风":
            value3 = 2
        elif fan_mode == "高风":
            value3 = 3
        else:
            value3 = 1  # 默认低风
        
        # 获取其他当前参数
        value1 = device_state.get("value1", 0)  # 当前开关状态
        value2 = device_state.get("value2", 3)  # 当前模式
        value4 = device_state.get("value4", 0)  # 当前温度值
        
        # 发送控制指令
        await self.coordinator.async_air_conditioner_state_update(self.device_id, value1, value2, value3, value4)

    @callback
    def _handle_coordinator_update(self) -> None:
        """当协调器通知更新时刷新状态"""
        if self.coordinator.device_states:
            device_state = self.coordinator.device_states.get(self.device_id, {})
            
            # 更新开关状态和HVAC模式
            is_on = device_state.get("state", False)
            value2 = device_state.get("value2", 3)  # 获取模式值，默认为制冷
            
            if not is_on:
                self._attr_hvac_mode = HVACMode.OFF
            elif value2 == 2:
                self._attr_hvac_mode = HVACMode.DRY  # value2=2实际对应除湿模式
            elif value2 == 7:
                self._attr_hvac_mode = HVACMode.FAN_ONLY  # value2=7实际对应仅送风模式
            elif value2 == 3:
                self._attr_hvac_mode = HVACMode.COOL
            elif value2 == 4:
                self._attr_hvac_mode = HVACMode.HEAT
            else:
                self._attr_hvac_mode = HVACMode.OFF
            
            # 更新温度
            self._attr_target_temperature = device_state.get("target_temperature", 25)
            self._attr_current_temperature = device_state.get("current_temperature", 25)
            
            # 更新风速
            value3 = device_state.get("value3", 1)
            if value3 == 1:
                self._attr_fan_mode = "低风"
            elif value3 == 2:
                self._attr_fan_mode = "中风"
            elif value3 == 3:
                self._attr_fan_mode = "高风"
            else:
                self._attr_fan_mode = "低风"
        
        self.async_write_ha_state()

    @property
    def should_poll(self) -> bool:
        return False        # 禁用轮询，依赖Coordinator推送更新

    async def async_added_to_hass(self):
        self.async_on_remove(self.coordinator.async_add_listener(self.async_write_ha_state))

    async def async_will_remove_from_hass(self):
        """实体移除时，停止Coordinator"""
        await self.coordinator.stop()
