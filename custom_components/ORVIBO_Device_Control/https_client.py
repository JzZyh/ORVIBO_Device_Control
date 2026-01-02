#!/usr/bin/env python
# -*- coding: utf-8 -*-

import logging
import json
import ssl
import asyncio
import aiohttp
from homeassistant.core import HomeAssistant  #引入HA核心类
from typing import Optional, Any
from .packet import HomemateJsonData
from .const import (
    ID_UNSET,
    ORVIBO_SWITCH_MODEL,
    HTTP_HEADERS
)
from .hass import  (
    get_name_by_id,
    get_uid_by_id,
    get_model_by_id,
    get_room_id_by_id,
    deduplicate_by_key,
    set_current_floor,
    set_current_family,
    set_current_rooms,
    set_current_devices,
    set_current_state,
    get_current_devices,
    get_current_state,
)


# 配置日志
_LOGGER = logging.getLogger(__name__)


class HttpsClient():
    def __init__(
            self,
            hass: HomeAssistant,
            username: str,
            password: str
    ):
        self.hass = hass
        self.username = username
        self.password = password

        self.user_id = None
        self.session_id: Optional[str] = None  # 从SSL客户端接收
        self.access_token: Optional[str] = None
        self.family_id: Optional[str] = None  # 传递给SSL客户端
        self.family_name: Optional[str] = None
        self.room_id: Optional[str] = None

        self.proxy = ""
        self.session: Optional[aiohttp.ClientSession] = None

    @property
    def is_logged_in(self) -> bool:
        """判断是否已登录（含令牌有效性）"""
        return self.access_token is not None and self.user_id is not None

    async def _create_ssl_context(self):
        """用 Python 标准库异步执行 SSL 同步操作，无需 hass 实例"""

        def _sync_create_context():
            """同步创建 SSL 上下文（原阻塞操作）"""
            ssl_context = ssl.create_default_context()
            # 保留你原有的调试配置（生产环境需改为 True + CERT_REQUIRED）
            ssl_context.check_hostname = False  # ⚠️ 仅调试用！
            ssl_context.verify_mode = ssl.CERT_NONE  # 配合调试关闭校验
            return ssl_context

        # 自动将同步函数放到线程池执行，不阻塞事件循环
        return await asyncio.to_thread(_sync_create_context)


    async def _connect(self):
        if self.session:
            return

        #ssl_context = ssl.create_default_context()
        ssl_context = await self._create_ssl_context()
        ssl_context.check_hostname = False  # ⚠️ 仅调试用！生产环境必须为 True
        connector = aiohttp.TCPConnector(ssl=ssl_context)

        self.session = aiohttp.ClientSession(connector=connector)
        _LOGGER.debug("HTTPS 会话创建成功")

    async def _disconnect(self):
        """关闭 HTTP 会话"""
        if self.session and not self.session.closed:
            await self.session.close()
            self.session = None
            _LOGGER.debug("HTTPS 会话关闭")
        self.access_token = None

    def set_session_id(self, session_id: str):
        """接收SSL客户端的session_id（线程安全）"""
        self.session_id = session_id

    async def _send_request(self, url, data):
        if not self.session:
            raise ConnectionError("客户端未连接")
        
        max_retries = 3
        retry_delay = 1  # 秒
        
        for attempt in range(max_retries):
            try:
                if not data:
                    resp = await self.session.get(
                        url=url,
                        headers=HTTP_HEADERS,
                        skip_auto_headers=["Accept", "Connection"],
                        proxy=self.proxy,
                        ssl=False  # 如果证书有问题可临时关闭验证（生产环境建议修复）
                    )
                else:
                    resp = await self.session.post(
                        url=url,
                        timeout=aiohttp.ClientTimeout(total=10),
                        data=data,
                        headers=HTTP_HEADERS,
                        skip_auto_headers=["Accept", "Connection"],
                        proxy=self.proxy,
                        ssl=False  # 如果证书有问题可临时关闭验证（生产环境建议修复）
                    )
                
                resp.raise_for_status()
                data = await resp.text()
                _LOGGER.debug(f"服务器原始响应数据: {data}")
                resp = json.loads(data)
                return resp
            except aiohttp.ClientResponseError as e:
                # 只对特定的HTTP错误进行重试
                if e.status in [502, 503, 504] and attempt < max_retries - 1:
                    _LOGGER.warning(f"HTTP请求失败，正在重试 ({attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(retry_delay)
                else:
                    raise
            except aiohttp.ClientError as e:
                # 对其他网络错误进行重试
                if attempt < max_retries - 1:
                    _LOGGER.warning(f"网络请求失败，正在重试 ({attempt + 1}/{max_retries}): {e}")
                    await asyncio.sleep(retry_delay)
                else:
                    raise

    async def ensure_login(self) -> bool:
        """确保已登录（自动刷新 token）"""
        if not self.is_logged_in:
            if not self.session:
                await self._connect()
        assert self.session is not None

        if not self.access_token or not self.user_id:
            data = await self._fetch_access_token()
            if data:
                self.access_token = data.get("access_token", "")
                self.user_id = data.get("user_id", "")
        assert self.access_token and self.user_id

        if not self.family_id:
            data = await self._fetch_https_family()
            if data:
                self.family_id = data.get("familyId", "")
                self.family_name = data.get("familyName", "")
        assert self.family_id
        return True

    async def _fetch_access_token(self) -> dict:
        try:
            if self.session_id is None or self.session_id == bytes(ID_UNSET).decode('utf-8'):
                ret = HomemateJsonData.get_access_token_by_password(self.username, self.password)
            else:
                ret = HomemateJsonData.get_access_token_by_session_id(self.session_id)
            resp = await self._send_request(ret['url'], ret['data'])
            if "message" in resp:
                _LOGGER.error(resp["message"])
                return {}
            if "data" not in resp:
                _LOGGER.error("响应包中未找到[data]")
                return {}
            if "access_token" not in resp["data"]:
                _LOGGER.error("响应包中未找到[access_token]")
                return {}
            _LOGGER.info("HTTPS 申请ACCESS_TOKEN成功")
            return resp["data"]
        except aiohttp.ClientError as e:
            _LOGGER.error("HTTPS请求失败: %s, URL: %s", e, ret['url'])
            return {}
        except Exception as e:
            _LOGGER.error("HTTPS请求失败: %s", e)
            return {}

    async def _fetch_https_family(self) -> dict:
        try:
            if not self.user_id or not self.access_token:
                _LOGGER.error("缺少[userId]或[accessToken]")
                return {}
            ret = HomemateJsonData.get_family_statistics_users(self.user_id, self.access_token)
            resp = await self._send_request(ret['url'], ret['data'])
            if "message" in resp:
                _LOGGER.error(resp["message"])
                return {}
            if "data" not in resp:
                _LOGGER.error("响应包中未找到[data]")
                return {}
            data = resp["data"]
            if isinstance(data, list) and len(data) > 0:
                data = data[0]
            if "familyId" not in data:
                _LOGGER.error("响应包中未找到[familyId]")
                return {}
            return data
        except aiohttp.ClientError as e:
            _LOGGER.error("HTTPS 请求失败: %s, URL: %s", e, ret['url'])
            return {}
        except Exception as e:
            _LOGGER.error("HTTPS 请求失败: %s", e)
            return {}

    async def _fetch_device_status(self, access_token, session_id, user_id, user_name, family_id) -> dict:
        try:
            ret = HomemateJsonData.get_devices_status(access_token=access_token,
                                                      session_id=session_id,
                                                      user_id=user_id,
                                                      user_name=user_name,
                                                      family_id=family_id)
            resp = await self._send_request(ret['url'], ret['data'])
            if "message" in resp:
                _LOGGER.error(resp["message"])
                return {}
            if "data" not in resp:
                _LOGGER.error("响应包中未找到[data]")
                return {}
            if "deviceStatus" not in resp["data"]:
                _LOGGER.error("响应包中未找到[deviceStatus]")
                return {}
            return resp["data"]
        except aiohttp.ClientError as e:
            _LOGGER.error("HTTPS 请求失败: %s, URL: %s", e, ret['url'])
            return {}
        except Exception as e:
            _LOGGER.error("HTTPS 请求失败: %s", e)
            return {}

    async def _fetch_https_homepage(self, family_id, user_id, access_token) -> dict:
        try:
            ret = HomemateJsonData.get_homepage_data(family_id=family_id,
                                                     user_id=user_id,
                                                     access_token=access_token)
            resp = await self._send_request(ret['url'], ret['data'])
            if "message" in resp:
                _LOGGER.error(resp["message"])
                return {}
            if "data" not in resp:
                _LOGGER.error("响应包中未找到[data]")
                return {}
            if "device" not in resp["data"]:
                _LOGGER.error("响应包中未找到[device]")
                return {}
            return resp["data"]
        except aiohttp.ClientError as e:
            _LOGGER.error("HTTPS 请求失败: %s, URL: %s", e, ret['url'])
            return {}
        except Exception as e:
            _LOGGER.error("HTTPS 请求失败: %s", e)
            return {}

    async def fetch_device_state(self)->bool:
        """周期性获取设备状态，所需参数：access_token,session_id,user_id,username,family_id"""
        try:
            if self.session_id == bytes(ID_UNSET).decode('utf-8'):
                _LOGGER.error("session_id 缺失")
                return False

            if not await self.ensure_login():
                _LOGGER.error("HTTPS 未登录")
                return False
            data = await self._fetch_device_status(
                                    self.access_token,
                                    self.session_id,
                                    self.user_id,
                                    self.username,
                                    self.family_id)
            assert data
            device = data.get("device", [])
            if isinstance(device, list) and len(device) > 0:
                device = device[0]
            self.room_id = device.get("roomId", "")
            _state_list = data.get("deviceStatus", [])
            if _state_list:
                # 只保留switch设备的状态
                device_list = get_current_devices(self.hass) or []
                switch_id_list = [item['deviceId'] for item in device_list if 'deviceId' in item]
                if switch_id_list:
                    _state_list = [item for item in _state_list if item.get('deviceId') in switch_id_list]
                _state_list = deduplicate_by_key(_state_list, 'deviceId')
                set_current_state(self.hass, _state_list)
                return True
            return False
        except aiohttp.ClientError as e:
            _LOGGER.error("拉取设备状态失败（网络错误）：%s",e)
            return False
        except Exception as e:
            _LOGGER.error("拉取设备状态失败：%s",e)
            return False

    async def fetch_homepage_data(self)->bool:
        """获取首页数据，所需参数：family_id,user_id,access_token"""
        try:
            if not await self.ensure_login():
                _LOGGER.error("HTTPS 未登录")
                return False

            data = await self._fetch_https_homepage(self.family_id, self.user_id, self.access_token)
            assert data

            device_list = data.get("device", [])
            state_list = data.get("deviceStatus", [])

            set_current_floor(self.hass, data.get("floor", [{}])[0] if data.get("floor") else {})
            set_current_family(self.hass, data.get("familyConfig", [{}])[0] if data.get("familyConfig") else {})
            set_current_rooms(self.hass, data.get("room", []))

            # 确保device_list是一个列表
            if not isinstance(device_list, list):
                device_list = []
                _LOGGER.warning("设备列表不是预期的列表类型")
            
            # 记录所有设备型号和详细信息
            all_models = set([item.get('model') for item in device_list if item.get('model')])
            _LOGGER.debug(f"所有设备型号: {all_models}")
            _LOGGER.debug(f"已配置的型号映射: {list(ORVIBO_SWITCH_MODEL.items())}")
            
            # 记录所有设备的详细信息
            for device in device_list:
                device_id = device.get('deviceId')
                model = device.get('model')
                device_name = device.get('deviceName')
                device_type = ORVIBO_SWITCH_MODEL.get(model, "未知类型")
                _LOGGER.debug(f"设备ID: {device_id}, 名称: {device_name}, 型号: {model}, 类型: {device_type}")
            
            # 不按型号过滤设备，而是记录所有设备
            _LOGGER.debug(f"发现的设备总数: {len(device_list)}")
            if not device_list:
                return False
            
            # 检查并去重设备ID，优先保留delFlag=0的设备
            _LOGGER.debug("原始设备列表: %s", device_list)
            device_list = deduplicate_by_key(device_list, 'deviceId')
            _LOGGER.debug("去重后的设备列表: %s", device_list)
            
            # 记录设备列表结构和UID-device_id映射
            _LOGGER.debug("设备列表结构和UID-device_id映射:")
            for device in device_list:
                device_id = device.get('deviceId')
                uid = device.get('uid')
                device_name = device.get('deviceName')
                _LOGGER.debug("设备: ID=%s, UID=%s, 名称=%s, 模型=%s, 房间ID=%s", 
                              device_id, uid, device_name, device.get('model'), device.get('roomId'))
                _LOGGER.debug("UID %s 映射到设备ID %s", uid, device_id)
            
            # 只保留switch设备的状态
            switch_id_list = [item['deviceId'] for item in device_list if 'deviceId' in item]
            if switch_id_list:
                state_list = [item for item in state_list if item.get('deviceId') in switch_id_list]
            state_list = deduplicate_by_key(state_list, 'deviceId')
            
            # 保存过滤和去重后的设备列表和状态列表
            set_current_devices(self.hass, device_list)
            set_current_state(self.hass, state_list)
            return True
        except aiohttp.ClientError as e:
            _LOGGER.error("获取主页数据失败（网络错误）：%s",e)
            return False
        except Exception as e:
            _LOGGER.error("获取主页数据失败：%s", e)
            return False

    async def update_state_list(self) -> None | dict[str, dict[str, Any]]:
        """
        拉取设备列表（核心方法）
        """
        try:
            device_list = get_current_devices(self.hass) or []
            if not device_list or not self.session_id:
                if not await self.fetch_homepage_data():
                    _LOGGER.debug("获取主页数据失败，尝试使用现有设备列表")
                    device_list = get_current_devices(self.hass) or []
                else:
                    device_list = get_current_devices(self.hass) or []
            else:
                await self.fetch_device_state()
            state_list = get_current_state(self.hass)


            if not device_list:
                _LOGGER.warning("设备列表为空")
                return {}
            
            if not state_list:
                _LOGGER.warning("状态列表为空")
                state_list = []

            _LOGGER.debug("获取到%d个设备，以及%d个设备状态", len(device_list), len(state_list))

            device_states = {}
            for state in state_list:
                device_id = state.get("deviceId", "")
                if not device_id:
                    continue
                    
                # 输出完整的设备状态信息，用于验证服务器返回的数据结构
                _LOGGER.debug(f"设备ID: {device_id} 的完整状态信息: {state}")
                
                # 获取设备类型
                device_model = get_model_by_id(self.hass, device_id)
                device_type = ORVIBO_SWITCH_MODEL.get(device_model, "Switch")
                
                # 解析设备状态值
                value1 = state.get("value1", 1)
                value2 = state.get("value2", 0)
                value3 = state.get("value3", 0)
                value4 = state.get("value4", 0)
                online = state.get("online", 1)
                
                # 根据设备类型使用不同的状态转换逻辑
                if device_type == "Ventilation":
                    # 新风设备：value1=0→慢（开），value1=50→停（关），value1=100→快（开）
                    status = False if value1 == 50 else True
                    # 根据value1设置风速档位
                    if value1 == 0:
                        fan_speed = "慢"
                    elif value1 == 50:
                        fan_speed = "停"
                    elif value1 == 100:
                        fan_speed = "快"
                    else:
                        fan_speed = "未知"
                elif device_type == "Air Conditioner":
                    # 空调设备：value1=1→关，value1=0→开
                    status = False if value1 == 1 else True
                else:
                    # 默认逻辑：value1=0→开，其他→关
                    status = value1 == 0
                
                # 解析value4为目标温度和室内温度
                target_temperature = (value4 >> 16) // 100
                indoor_temperature = (value4 & 0xFFFF) // 100
                
                _LOGGER.debug(f"设备ID: {device_id}, value1: {value1}, 转换后状态: {status}, online: {online}")
                _LOGGER.debug(f"模式: {value2}, 风速: {value3}, 目标温度: {target_temperature}°C, 室内温度: {indoor_temperature}°C")

                device_name = get_name_by_id(self.hass, device_id)
                device_uid = get_uid_by_id(self.hass, device_id)
                device_model = get_model_by_id(self.hass, device_id)
                room_id = get_room_id_by_id(self.hass, device_id)
                
                _LOGGER.debug("处理设备状态: device_id=%s, device_name=%s, device_uid=%s, status=%s", 
                              device_id, device_name, device_uid, status)
                
                if device_name:
                    device_states[device_id] = {
                        "device_id": device_id,
                        "device_name": device_name,
                        "device_uid": device_uid,
                        "model": device_model,
                        "state": status,
                        "online": online,
                        "room_id": room_id,
                        "value1": value1,  # 原始值：0为慢，50为停，100为快
                        "value2": value2,  # 原始值
                        "value3": value3,  # 原始值
                        "value4": value4,  # 原始值
                        "current_temperature": indoor_temperature,  # 解析后的室内温度
                        "target_temperature": target_temperature,  # 解析后的目标温度
                        "mode": value2,  # 保存模式
                        "fan_speed": fan_speed if device_type == "Ventilation" else value3  # 保存风速
                    }
            
            # 为所有在设备列表中但不在设备状态中的设备创建基本状态
            device_ids_in_states = set(device_states.keys())
            for device in device_list:
                device_id = device.get("deviceId")
                if device_id and device_id not in device_ids_in_states:
                    _LOGGER.warning(f"为设备{device_id}创建默认状态")
                    device_states[device_id] = {
                        "device_id": device_id,
                        "device_name": device.get("deviceName", "未知设备"),
                        "device_uid": device.get("uid", ""),
                        "model": device.get("model", ""),
                        "state": False,
                        "online": 1,
                        "room_id": device.get("roomId", ""),
                    }
            
            return device_states
        except aiohttp.ClientError as e:
            _LOGGER.error("拉取设备失败（网络错误）：%s", e)
            return None
        except Exception as e:
            _LOGGER.error("拉取设备失败：%s", e)
            return None

def test():
    print("Test")


if __name__ == '__main__':
    test()
