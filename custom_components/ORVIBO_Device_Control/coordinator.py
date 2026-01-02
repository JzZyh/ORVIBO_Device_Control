# custom_components/wifi_switch/coordinator.py
import logging
import asyncio

from typing import Dict, Any
from datetime import timedelta
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.update_coordinator import UpdateFailed
from homeassistant.helpers.event import async_track_time_interval

from .ssl_client import SSLClient
from .https_client import (
    HttpsClient
)


from .const import (
    SSL_HOST, SSL_PORT,
    DEVICE_NAME,
    UPDATE_INTERVAL,
    SSL_RECONNECT_INTERVAL,
    ORVIBO_SWITCH_MODEL
)

_LOGGER = logging.getLogger(__name__)


class OrviboSwitchCoordinator(DataUpdateCoordinator[Dict[str, Any]]):
    _initial_keys: Dict[str, Any] = {}
    def __init__(self, hass: HomeAssistant, username: str, password: str):
        self.username = username
        self.password = password
        self.hass = hass

        self.https_client = HttpsClient(
                        hass=hass,
                        username=username,
                        password=password
        )
        self.ssl_client = None

        super().__init__(
            hass,
            _LOGGER,
            name=f"{DEVICE_NAME} Coordinator",
            update_interval=UPDATE_INTERVAL,
        )

        self.device_states: Dict[str, Any] = {}

    async def _async_setup(self):
        """Set up the coordinator

        This is the place to set up your coordinator,
        or to load data, that only needs to be loaded once.

        This method will be called automatically during
        coordinator.async_config_entry_first_refresh.
        """
        try:
            # 1. 确保HTTPS登录（获取family_id）
            if not await self.https_client.ensure_login():
                raise UpdateFailed("HTTPS登录失败")

            # 2.首次拉取所有设备信息
            device_states = await self.https_client.update_state_list()
            # 确保device_states至少是一个空字典
            if device_states is None:
                self.device_states = {}
            else:
                # 过滤掉delFlag为1的设备，保留online为0的设备以便显示为不可用状态
                self.device_states = {
                    device_id: state 
                    for device_id, state in device_states.items() 
                    if state.get('delFlag') != 1
                }

            # 2. 初始化全局SSL客户端（仅创建1次）
            await self._init_ssl_client()

            if self.ssl_client:
                # 启动SSL连接
                # self.hass.async_create_task(self.ssl_client.connect_and_login())
                await self.ssl_client.connect_and_login()
        except Exception as e:
            raise UpdateFailed(f"拉取设备状态失败: {str(e)}") from e

    async def _async_update_data(self) -> Dict[str, Any]:
        """定期拉取所有设备状态"""
        _LOGGER.debug("正在获取设备及状态数据...")
        try:
            # 1. 确保HTTPS登录（获取family_id）
            if not await self.https_client.ensure_login():
                raise UpdateFailed("HTTPS登录失败")

            # 2. 获取设备最新状态（首次执行会同时拉取所有设备信息）
            device_states = await self.https_client.update_state_list()
            if device_states:
                # 过滤掉delFlag为1的设备，保留online为0的设备以便显示为不可用状态
                self.device_states = {
                    device_id: state 
                    for device_id, state in device_states.items() 
                    if state.get('delFlag') != 1
                }
            if not self.device_states:
                raise UpdateFailed("未获取到设备信息")
            return self.device_states
        except Exception as e:
            raise UpdateFailed(f"拉取设备状态失败: {str(e)}") from e

    async def _init_ssl_client(self):
        """初始化全局SSL客户端（仅执行1次）"""
        if self.ssl_client is not None:
            return

        # 确保HTTPS已获取family_id
        while True:
            if not self.https_client.family_id:
                _LOGGER.error("初始化SSL客户端失败：缺少family_id")
                await asyncio.sleep(1)
                continue
            break

        # 定义回调函数
        def on_session_id_obtained(session_id: str):
            """SSL session_id 回传回调"""
            _LOGGER.debug("为https_client设置session_id: %s", session_id)
            self.https_client.set_session_id(session_id)

        def on_status_update(device_id: str, status: int, value2: int = 0, value3: int = 0, value4: int = 0):
            """SSL状态推送回调"""
            # 获取设备类型
            device_state = self.device_states.get(device_id, {})
            model = device_state.get('model')
            device_type = ORVIBO_SWITCH_MODEL.get(model, "Switch")
            
            # 更新基本状态
            self.device_states[device_id]["value1"] = status
            # 更新相关状态字段
            self.device_states[device_id]["value2"] = value2  # 模式
            self.device_states[device_id]["value3"] = value3  # 风速
            self.device_states[device_id]["value4"] = value4  # 温度
            
            # 针对不同设备类型的特殊处理
            if device_type == "Ventilation":
                # 新风设备状态日志
                _LOGGER.debug(f"新风设备 {device_id} 状态更新: value1={status}, value2={value2}, value3={value3}, value4={value4}")
                # 根据实际日志分析，新风设备的风速档位由value1控制
                # 实际操控顺序：慢 → 快 → 停
                # 正确映射关系：value1=0 → 慢，value1=50 → 停，value1=100 → 快
                if status == 0:
                    is_on = True
                    fan_speed = "慢"
                elif status == 50:
                    is_on = False
                    fan_speed = "停"
                elif status == 100:
                    is_on = True
                    fan_speed = "快"
                else:
                    # 未知状态
                    is_on = (status != 50)
                    fan_speed = "未知"
                _LOGGER.debug(f"新风设备 {device_id} 状态解析: 开启状态={is_on}, 风速档位={fan_speed}")
                # 更新设备状态
                self.device_states[device_id]["state"] = is_on
                self.device_states[device_id]["fan_speed"] = fan_speed
            elif device_type == "Air Conditioner":
                # 空调设备温度解析
                is_on = (status == 0)
                self.device_states[device_id]["state"] = is_on
                if value4 > 0:
                    target_temperature = (value4 >> 16) // 100
                    current_temperature = (value4 & 0xFFFF) // 100
                    self.device_states[device_id]["target_temperature"] = target_temperature
                    self.device_states[device_id]["current_temperature"] = current_temperature
                    # _LOGGER.info(f"设备 {device_id} 温度解析 - 目标温度: {target_temperature}°C, 当前温度: {current_temperature}°C")
            else:
                # 其他设备默认处理
                is_on = (status == 0)
                self.device_states[device_id]["state"] = is_on
            
            self.async_set_updated_data(self.device_states)

        # 创建全局SSL客户端
        self.ssl_client = SSLClient(
            hass=self.hass,
            ssl_host=SSL_HOST,
            ssl_port=SSL_PORT,
            username=self.username,
            password=self.password,
            family_id=self.https_client.family_id,
            on_status_update=on_status_update,
            on_session_id_obtained=on_session_id_obtained,
            retry_interval = SSL_RECONNECT_INTERVAL
        )

    async def toggle_switch(self, device_id: str) -> bool:
        """发送控制指令"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化，无法发送控制指令")
            return False
        await self.ssl_client.async_toggle_device(device_id)
        return True

    async def async_turn_on(self, device_id: str) -> bool:
        """发送开启指令"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化，无法发送控制指令")
            return False
        result = await self.ssl_client.async_turn_on(device_id)
        # 更新本地状态
        if result and self.device_states and device_id in self.device_states:
            self.device_states[device_id]["state"] = True
            self.async_set_updated_data(self.device_states)
        return result

    async def async_turn_off(self, device_id: str) -> bool:
        """发送关闭指令"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化，无法发送控制指令")
            return False
        result = await self.ssl_client.async_turn_off(device_id)
        # 更新本地状态
        if result and self.device_states and device_id in self.device_states:
            self.device_states[device_id]["state"] = False
            self.async_set_updated_data(self.device_states)
        return result

    def get_device_state(self, device_id):
        if self.device_states is None:
            return False
        return self.device_states.get(device_id).get("state")

    async def async_control_air_conditioner(self, device_id: str, value1: int, value2: int, value3: int, value4: int) -> bool:
        """发送空调控制指令"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化，无法发送控制指令")
            return False
        result = await self.ssl_client.async_control_air_conditioner(device_id, value1, value2, value3, value4)
        # 更新本地状态
        if result and self.device_states and device_id in self.device_states:
            # 更新基本状态
            self.device_states[device_id]["value1"] = value1
            self.device_states[device_id]["state"] = (value1 == 0)
            self.device_states[device_id]["value2"] = value2
            self.device_states[device_id]["value3"] = value3
            self.device_states[device_id]["value4"] = value4
            
            # 更新解析后的温度
            target_temperature = (value4 >> 16) // 100
            indoor_temperature = (value4 & 0xFFFF) // 100
            self.device_states[device_id]["target_temperature"] = target_temperature
            self.device_states[device_id]["current_temperature"] = indoor_temperature
            
            self.async_set_updated_data(self.device_states)
        return result
    
    async def async_air_conditioner_state_update(self, device_id: str, value1: int, value2: int, value3: int, value4: int) -> bool:
        """使用CMD_STATE_UPDATE命令发送空调控制指令"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化，无法发送控制指令")
            return False
        result = await self.ssl_client.async_air_conditioner_state_update(device_id, value1, value2, value3, value4)
        # 更新本地状态
        if result and self.device_states and device_id in self.device_states:
            # 更新基本状态
            self.device_states[device_id]["value1"] = value1
            self.device_states[device_id]["state"] = (value1 == 0)
            self.device_states[device_id]["value2"] = value2
            self.device_states[device_id]["value3"] = value3
            self.device_states[device_id]["value4"] = value4
            
            # 更新解析后的温度
            target_temperature = (value4 >> 16) // 100
            indoor_temperature = (value4 & 0xFFFF) // 100
            self.device_states[device_id]["target_temperature"] = target_temperature
            self.device_states[device_id]["current_temperature"] = indoor_temperature
            
            self.async_set_updated_data(self.device_states)
        return result
    
    async def async_control_ventilation(self, device_id: str, value1: int) -> bool:
        """发送新风设备控制指令"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化，无法发送控制指令")
            return False
        result = await self.ssl_client.async_control_ventilation(device_id, value1)
        # 更新本地状态
        if result and self.device_states and device_id in self.device_states:
            # 根据value1更新设备状态
            if value1 == 0:
                # 慢档
                is_on = True
                fan_speed = "慢"
            elif value1 == 50:
                # 停止
                is_on = False
                fan_speed = "停"
            elif value1 == 100:
                # 快档
                is_on = True
                fan_speed = "快"
            else:
                # 未知状态
                is_on = (value1 != 50)
                fan_speed = "未知"
            
            # 更新设备状态
            self.device_states[device_id]["value1"] = value1
            self.device_states[device_id]["state"] = is_on
            self.device_states[device_id]["fan_speed"] = fan_speed
            
            self.async_set_updated_data(self.device_states)
        return result
    
    async def async_ventilation_state_update(self, device_id: str, value1: int) -> bool:
        """使用CMD_STATE_UPDATE命令发送新风设备控制指令"""
        if not self.ssl_client:
            _LOGGER.error("SSL客户端未初始化，无法发送控制指令")
            return False
        result = await self.ssl_client.async_ventilation_state_update(device_id, value1)
        # 更新本地状态
        if result and self.device_states and device_id in self.device_states:
            # 根据value1更新设备状态
            if value1 == 0:
                # 慢档
                is_on = True
                fan_speed = "慢"
            elif value1 == 50:
                # 停止
                is_on = False
                fan_speed = "停"
            elif value1 == 100:
                # 快档
                is_on = True
                fan_speed = "快"
            else:
                # 未知状态
                is_on = (value1 != 50)
                fan_speed = "未知"
            
            # 更新设备状态
            self.device_states[device_id]["value1"] = value1
            self.device_states[device_id]["state"] = is_on
            self.device_states[device_id]["fan_speed"] = fan_speed
            
            self.async_set_updated_data(self.device_states)
        return result

    async def async_cleanup(self):
        """组件卸载时清理资源"""
        if self.ssl_client:
            await self.ssl_client.disconnect()
            _LOGGER.debug("全局SSL连接已清理")
