#!/usr/bin/env python
# -*- coding: utf-8 -*-

import os
import logging
import ssl
import asyncio
import time
from pathlib import Path
from datetime import datetime
from typing import Optional, Callable
from homeassistant.core import HomeAssistant  #引入HA核心类
from .packet import (HomematePacket, HomemateJsonData)

from.hass import (
    get_uid_by_id,
    get_id_by_uid,
    get_name_by_uid,
    get_name_by_id,
    get_current_devices,
    get_current_state,
    get_state_by_id,
    set_state_by_id,
    set_state_by_uid
)

from .const import (
    SSL_HOST, SSL_PORT, CLIENT_CERT, CLIENT_KEY, SERVER_CA, ID_UNSET, DEFAULT_KEY,
    SSL_MAX_RECONNECT_ATTEMPTS,
    CMD_HELLO, CMD_LOGIN, CMD_STATE_UPDATE, CMD_CONTROL, CMD_HEARTBEAT, CMD_HANDSHAKE,
)

_LOGGER = logging.getLogger(__name__)

class SSLClient:
    _initial_keys: dict[str, bytes] = {}
    """独立的SSL长连接客户端：处理SSL连接、登录、控制指令发送、状态监听"""
    def __init__(
        self,
        hass: HomeAssistant,
        ssl_host: str,
        ssl_port: int,
        username: str,
        password: str,
        family_id: str,
        on_session_id_obtained: Callable[[str], None],
        on_status_update: Callable[[str, int, int, int, int], None],
        heartbeat_interval: int = 30,
        retry_interval: int = 5
    ):
        """
        初始化SSL长连接客户端
        :param hass: Home Assistant实例（用于线程池执行同步操作）
        :param ssl_host: SSL服务器地址
        :param ssl_port: SSL服务器端口
        :param username: 登录用户名
        :param password: 登录密码
        :param family_id: 家庭id号
        :param on_session_id_obtained: 获取到session_id后回调
        :param on_status_update: 状态更新回调（参数：device_id, status, value2, value3, value4）
        :param heartbeat_interval: 心跳包发送间隔（秒）
        :param retry_interval: 重连间隔（秒）
        """
        self.hass = hass  # 存储HA实例
        self.ssl_host = ssl_host
        self.ssl_port = ssl_port
        self.username = username
        self.password = password
        self.family_id = family_id

        self.on_session_id_obtained = on_session_id_obtained
        self.on_status_update = on_status_update
        self.heartbeat_interval = heartbeat_interval
        self.retry_interval = retry_interval
        self._heartbeat_task = None  # 心跳任务

        BASE_DIR = Path(__file__).parent.resolve()
        self.certfile=BASE_DIR / CLIENT_CERT
        self.keyfile=BASE_DIR / CLIENT_KEY
        self.cafile=BASE_DIR / SERVER_CA

        # 连接状态
        self.ssl_context = None
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.session_id: Optional[str] = None
        self.session_key: Optional[bytes] = None
        self.connected: bool = False
        self._listening_task: Optional[asyncio.Task] = None

    @classmethod
    def add_key(cls, session_id: str, key: bytes):
        cls._initial_keys[session_id] = key

    @classmethod
    def get_key(cls, session_id:str) -> bytes:
        try:
            return cls._initial_keys[session_id]
        except KeyError:
            return DEFAULT_KEY.encode("utf-8")

    @property
    def is_connected(self):
        return self.connected

    async def _create_ssl_context(self):
        """异步创建SSL上下文（通过HA线程池执行同步操作）"""
        def _sync_create_context():
            try:
                if not os.path.exists(self.certfile):
                    raise FileNotFoundError("找不到证书文件：%s", self.certfile)
                if not os.path.exists(self.keyfile):
                    raise FileNotFoundError("找不到密钥文件：%s", self.keyfile)
                if not os.path.exists(self.cafile):
                    raise FileNotFoundError("找不到CA证书文件：%s", self.cafile)
                context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
                context.load_cert_chain(certfile=self.certfile, keyfile=self.keyfile)
                context.load_verify_locations(cafile=self.cafile)
                context.check_hostname = True
                context.verify_mode = ssl.CERT_REQUIRED
                # self.ssl_context = context
                return context
            except Exception as e:
                _LOGGER.error(f"创建SSL上下文失败: {str(e)}")
                raise

        return await self.hass.async_add_executor_job(_sync_create_context)

    async def _connect(self):
        """建立SSL连接（消除阻塞警告）（先确保上下文已创建）"""
        if self.connected:
            return True
        try:
            if not self.ssl_context:
                self.ssl_context = await self._create_ssl_context()
            _LOGGER.debug("SSL正在连接...")
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_connection(
                    host=self.ssl_host,
                    port=self.ssl_port,
                    ssl=self.ssl_context,
                    server_hostname=self.ssl_host
                ),
                timeout=10.0  # 10秒超时
            )
            self._update_activity("SSL连接成功")
            self.connected = True
            return True
        except asyncio.TimeoutError:
            _LOGGER.error("SSL连接服务器 [%s:%s] 超时", SSL_HOST, SSL_PORT)
            return False
        except OSError as e:
            _LOGGER.error("SSL连接发生IO错误: %s", e)
            return False
        except Exception as e:
            _LOGGER.error("SSL连接失败: %s", e)
            return False

    async def _disconnect(self):
        """退出监听任务并断开连接"""
        # 取消心跳任务
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        if self._listening_task and not self._listening_task.done():
            self._listening_task.cancel()
            try:
                await self._listening_task
            except asyncio.CancelledError:
                pass

        if self.writer and not self.writer.is_closing():
            _LOGGER.debug("SSL正在断开已有连接...")
            self.writer.close()
            try:
                await asyncio.wait_for(self.writer.wait_closed(), timeout=2.0)
            except asyncio.TimeoutError:
                _LOGGER.debug("关闭SSL连接超时")
            except Exception as e:
                _LOGGER.debug("关闭SSL连接失败: %s", e)

        self.reader = None
        self.writer = None
        self.session_id = None
        self.session_key = None
        self.connected = False
        _LOGGER.debug(f"SSL连接已断开")

    async def _reconnect(self):
        """重连逻辑"""
        try:
            await self._disconnect()
        except Exception as e:
            _LOGGER.error("错误: %s", e)

        if self.retry_interval > 0:
            _LOGGER.debug(f"{self.retry_interval}秒后尝试重连...")
            await asyncio.sleep(self.retry_interval)
            await self.connect_and_login()

    async def connect_and_login(self):
        """建立连接并完成登录流程"""
        if self.connected:
            return True
        for retry in range(SSL_MAX_RECONNECT_ATTEMPTS):
            try:
                # 建立 SSL 连接
                _LOGGER.debug("SSL正在连接和登录...")
                self.connected = await self._connect()
                if self.connected:
                    # 发送获取会话密钥请求
                    await self._send_hello()

                    # 启动监听任务
                    self._listening_task = self.hass.async_create_background_task(
                        self._listen_loop(),
                        name="server_response_listener")

                    # 等待获取session_id和session_key
                    await asyncio.sleep(3)
                    # SSL 登录
                    await self._send_login()
                    return True
            except Exception as e:
                _LOGGER.warning(f"连接/登录重试 {retry+1}/{SSL_MAX_RECONNECT_ATTEMPTS}")
                await asyncio.sleep(self.retry_interval * (retry + 1))  # 指数退避
        return False

    async def _send_packet(self, data: dict, key: bytes):
        """加密并发送数据包"""
        try:
            if key == DEFAULT_KEY.encode("utf-8"):
                packet_type = bytes([0x70, 0x6b])   #pk开头的使用默认密钥加密
                self.session_id = bytes(ID_UNSET).decode("utf-8")
            else:
                packet_type = bytes([0x64, 0x6b])   #dk开头的使用服务器会话密钥加密
            if not self.session_id:
                _LOGGER.error("会话ID为空，无法发送数据包")
                return
            ciphertext = HomematePacket.build_packet(
                packet_type=packet_type,
                key=key,
                session_id=self.session_id.encode("utf-8"),
                payload=data
            )
            if not self.writer:
                await self._reconnect()
            if not self.writer:
                _LOGGER.error("重连失败，无法发送指令")
                return
            self._update_activity("发送指令")
            self.writer.write(ciphertext)
            await self.writer.drain()
        except Exception as e:
            _LOGGER.error("发送失败: %s", e)
            if 'lost' in str(e) or 'close' in str(e) or '_write_appdata' in str(e):
                await self._reconnect()

    async def _send_hello(self):
        """发送申请会话密钥请求"""
        payload = HomemateJsonData.ssl_get_session()
        await self._send_packet(payload, DEFAULT_KEY.encode("utf-8"))

    async def _send_login(self):
        """发送登录请求"""
        if not self.connected:
            _LOGGER.warning(f"未建立SSL连接，无法登录")
            return False
        payload = HomemateJsonData.ssl_login(username=self.username,
                                             password_md5=self.password,
                                             family_id=self.family_id)
        if self.session_key and self.session_key != DEFAULT_KEY.encode("utf-8"):
            await self._send_packet(payload, self.session_key)
            # 启动心跳任务
            self._start_heartbeat_task()
            return True
        return False

    async def _send_control(self, device_id: str, device_uid: str, state: int, value2: int = 0, value3: int = 0, value4: int = 0):
        """发送控制指令，支持完整的空调参数"""
        await self.connect_and_login()
        # 移除assert检查，改为条件判断
        if not device_uid:
            _LOGGER.warning("设备%s没有UID信息，无法发送控制指令", device_id)
            return False

        payload = HomemateJsonData.ssl_switch_control(username=self.username,
                                                      device_id=device_id,
                                                      device_mac=device_uid,
                                                      state=state,
                                                      value2=value2,
                                                      value3=value3,
                                                      value4=value4)
        if self.session_key and self.session_key != DEFAULT_KEY.encode("utf-8"):
            for retry in range(SSL_MAX_RECONNECT_ATTEMPTS):
                if self.connected:
                    await self._send_packet(payload, self.session_key)
                    return True
                _LOGGER.warning("SSL连接未建立，2秒后重试...")
                await asyncio.sleep(2)
        _LOGGER.warning("无法给[%s]发送控制指令", device_id)
        return False

    async def async_control_air_conditioner(self, device_id: str, value1: int, value2: int, value3: int, value4: int):
        """控制空调设备的完整参数"""
        uid = get_uid_by_id(self.hass, device_id)
        if uid:
            # value1: 1为关，0为开
            result = await self._send_control(device_id, uid, state=1 if value1 == 1 else 0, value2=value2, value3=value3, value4=value4)
            if result:
                # 更新本地状态
                set_state_by_id(self.hass, device_id, value1)
                return True
        return False
    
    async def async_air_conditioner_state_update(self, device_id: str, value1: int, value2: int, value3: int, value4: int):
        """使用CMD_STATE_UPDATE命令更新空调设备的状态"""
        uid = get_uid_by_id(self.hass, device_id)
        if not uid:
            _LOGGER.warning("设备%s没有UID信息，无法发送状态更新指令", device_id)
            return False
            
        await self.connect_and_login()
        if not self.connected:
            _LOGGER.warning("SSL连接未建立，无法发送状态更新指令")
            return False
            
        # 构建CMD_STATE_UPDATE指令的payload
        payload = HomemateJsonData.ssl_air_conditioner_state_update(
            username=self.username,
            device_id=device_id,
            device_mac=uid,
            value1=value1,
            value2=value2,
            value3=value3,
            value4=value4
        )
        
        if self.session_key and self.session_key != DEFAULT_KEY.encode("utf-8"):
            for retry in range(SSL_MAX_RECONNECT_ATTEMPTS):
                if self.connected:
                    await self._send_packet(payload, self.session_key)
                    _LOGGER.debug("已发送空调状态更新指令: device_id=%s, value1=%s, value2=%s, value3=%s, value4=%s", 
                               device_id, value1, value2, value3, value4)
                    return True
                _LOGGER.warning("SSL连接未建立，2秒后重试...")
                await asyncio.sleep(2)
        _LOGGER.warning("无法给[%s]发送空调状态更新指令", device_id)
        return False
    
    async def async_control_ventilation(self, device_id: str, value1: int):
        """控制新风设备的风速"""
        uid = get_uid_by_id(self.hass, device_id)
        if uid:
            # value1: 0为慢档，50为停，100为快档
            result = await self._send_control(device_id, uid, state=value1, value2=0, value3=0, value4=0)
            if result:
                return True
        return False
    
    async def async_ventilation_state_update(self, device_id: str, value1: int):
        """使用CMD_STATE_UPDATE命令更新新风设备的状态"""
        uid = get_uid_by_id(self.hass, device_id)
        if not uid:
            _LOGGER.warning("设备%s没有UID信息，无法发送状态更新指令", device_id)
            return False
            
        await self.connect_and_login()
        if not self.connected:
            _LOGGER.warning("SSL连接未建立，无法发送状态更新指令")
            return False
            
        # 构建CMD_STATE_UPDATE指令的payload
        payload = HomemateJsonData.ssl_ventilation_state_update(
            username=self.username,
            device_id=device_id,
            device_mac=uid,
            value1=value1
        )
        
        if self.session_key and self.session_key != DEFAULT_KEY.encode("utf-8"):
            for retry in range(SSL_MAX_RECONNECT_ATTEMPTS):
                if self.connected:
                    await self._send_packet(payload, self.session_key)
                    _LOGGER.debug("已发送新风状态更新指令: device_id=%s, value1=%s", device_id, value1)
                    return True
                _LOGGER.warning("SSL连接未建立，2秒后重试...")
                await asyncio.sleep(2)
        _LOGGER.warning("无法给[%s]发送新风状态更新指令", device_id)
        return False

    def _start_heartbeat_task(self):
        """启动心跳任务"""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
        
        self._heartbeat_task = self.hass.async_create_background_task(
            self._send_heartbeat(),
            name="ssl_heartbeat_task"
        )

    async def _send_heartbeat(self):
        """定期发送心跳包"""
        _LOGGER.debug("心跳任务已启动，间隔: %d秒", self.heartbeat_interval)
        try:
            while self.connected:
                try:
                    payload = HomemateJsonData.ssl_heartbeat()
                    if self.session_key and self.session_key != DEFAULT_KEY.encode("utf-8"):
                        await self._send_packet(payload, self.session_key)
                        _LOGGER.debug("心跳包发送成功")
                    await asyncio.sleep(self.heartbeat_interval)
                except Exception as e:
                    _LOGGER.warning("发送心跳包失败: %s", e)
                    await asyncio.sleep(1)  # 短暂延迟后重试
        except asyncio.CancelledError:
            _LOGGER.debug("心跳任务已取消")
        except Exception as e:
            _LOGGER.error("心跳任务异常: %s", e)

    async def _listen_loop(self):
        """持续监听服务器消息"""
        _LOGGER.debug("已进入SSL服务器监听状态")
        try:
            while True:
                try:
                    # 读取42字节长度的头部数据
                    header_data = await self.reader.readexactly(42)
                    if not header_data:
                        await asyncio.sleep(1)
                        continue
                    length = HomematePacket.parse_length(header_data)
                    ciphertext = await self.reader.readexactly(length-42)
                    if self.session_key is None:
                        self.session_key = DEFAULT_KEY.encode("utf-8")
                    # 解密
                    packet = HomematePacket(header_data+ciphertext, {self.session_id: self.session_key})
                    self.session_id = bytes(packet.session_id).decode('utf-8')
                    data = packet.json_payload

                    cmd = data.get("cmd")
                    if cmd :
                        self._update_activity(f"收到服务器响应: cmd={cmd}")
                    if cmd == CMD_HELLO:
                        await self._handle_hello(data)
                    elif cmd == CMD_LOGIN:
                        await self._handle_login(data)
                    elif cmd == CMD_CONTROL:
                        await self._handle_control(data)
                    elif cmd == CMD_STATE_UPDATE:
                        await self._handle_state_update(data)
                    elif cmd == CMD_HANDSHAKE:
                        pass  # 忽略握手
                    elif cmd == CMD_HEARTBEAT:
                        pass  # 忽略心跳响应
                    else:
                        _LOGGER.warning("未知命令: %s", cmd)
                        _LOGGER.debug("响应包: %s", data)
                except asyncio.IncompleteReadError as e:
                    _LOGGER.warning("读取失败: %s，连接中断: %s", e, self.reader.at_eof())
                    break
                except asyncio.TimeoutError as e:
                    _LOGGER.warning("等待超时: %s，连接中断: %s", e, self.reader.at_eof())
                    break
                except ConnectionError as e:
                    _LOGGER.warning("连接错误: %s，连接中断: %s", e, self.reader.at_eof())
                    break
                except Exception as e:
                    _LOGGER.error("接收错误: %s，连接中断: %s", e, self.reader.at_eof())
                    break
        except asyncio.CancelledError:
            _LOGGER.debug("监听任务已取消")
        finally:
            _LOGGER.debug("已退出SSL服务器监听状态")
            # 断开后重连
            await self._reconnect()

    async def _handle_hello(self, data: dict):
        """处理会话密钥响应"""
        self.session_key = str(data.get("key")).encode("utf-8")
        if self.session_id:
            SSLClient.add_key(self.session_id, self.session_key)
            _LOGGER.debug("SSL 会话创建成功, sessionId: %s, sessionKey: %s",self.session_id, data.get("key"))
            self.on_session_id_obtained(self.session_id)

    async def _handle_login(self, data: dict):
        """处理登录响应"""
        if "userId" in data:
            _LOGGER.info("SSL 登录成功，userId: %s",data.get("userId"))
            self._connected = True
            # 可选：请求当前状态
        else:
            _LOGGER.error("SSL 登录失败: %s", data.get("msg"))

    async def _handle_control(self, data: dict):
        """处理开关控制响应"""
        if "uid" in data or "deviceId" in data:
            # 优先从响应数据中获取deviceId
            device_id = data.get("deviceId")
            device_name = None
            
            if device_id:
                device_name = get_name_by_id(self.hass, device_id)
                _LOGGER.debug("从deviceId获取设备名称: %s", device_name)
        
            # 如果deviceId不存在或获取设备名称失败，再从UID获取（保持兼容性）
            uid = data.get("uid") if "uid" in data else None
            if not device_name and uid:
                device_name = get_name_by_uid(self.hass, uid)
                _LOGGER.debug("从UID获取设备名称: %s", device_name)
                
            _LOGGER.debug("开关[%s]控制成功", device_name if device_name else device_id or uid)
        else:
            _LOGGER.warning("开关控制失败: %s", data.get("msg"))

    async def _handle_state_update(self, data: dict):
        """处理状态更新推送"""
        _LOGGER.debug("完整的状态更新数据: %s", data)
        if data.get("respByAcc"):
            # 优先从推送数据中获取deviceId
            device_id = data.get("deviceId","")
            device_state = data.get("value1",1)
            
            # 提取空调相关的状态字段
            value2 = data.get("value2", 0)  # 空调模式
            value3 = data.get("value3", 0)  # 风速
            value4 = data.get("value4", 0)  # 温度
            
            # 添加详细日志记录原始推送的所有关键状态字段
            _LOGGER.debug("设备状态更新原始数据 - deviceId: %s, value1(开关): %s, value2(模式): %s, value3(风速): %s, value4(温度): %s", 
                        device_id, device_state, value2, value3, value4)
            
            # 如果deviceId不存在，再从UID获取（保持兼容性）
            if not device_id:
                uid = data.get("uid","")
                _LOGGER.debug("接收到设备状态更新: UID=%s, 状态=%s", uid, device_state)
                device_id = get_id_by_uid(self.hass, uid)
                _LOGGER.debug("UID %s 映射到设备ID %s", uid, device_id)
            else:
                _LOGGER.debug("直接从推送数据中获取到设备ID: %s, 状态=%s", device_id, device_state)
            
            # 验证device_id是否有效
            if device_id:
                # 检查device_id是否存在于当前的设备列表中
                device_list = get_current_devices(self.hass)
                device_exists = any(device.get("deviceId") == device_id for device in device_list)
                _LOGGER.debug("设备ID %s 是否存在于设备列表中: %s", device_id, device_exists)
                
                if device_exists:
                    # 根据deviceId获取设备名称
                    device_name = get_name_by_id(self.hass, device_id)
                    _LOGGER.debug("设备ID %s 对应的设备名称: %s", device_id, device_name)
                    
                    # 更新状态列表 - 注释掉直接更新value1的代码，因为on_status_update会正确处理状态解析
                    # if set_state_by_id(self.hass, device_id, device_state):
                    #     _LOGGER.debug("开关[%s]状态更新为: %s", device_name, "关闭" if device_state==1 else "开启")
                    # 触发完整的状态更新回调，包含所有空调状态字段
                    _LOGGER.debug("触发设备ID %s 的完整状态更新", device_id)
                    self.on_status_update(device_id, device_state, value2, value3, value4)
                else:
                    _LOGGER.warning("设备ID %s 不存在于设备列表中，跳过状态更新", device_id)
            else:
                _LOGGER.warning("无法根据UID %s 找到对应的设备ID", uid)

    async def _handle_heartbeat(self, data: dict):
        """处理心跳包(未实现)"""
        if "uid" in data:
            uid = data.get("uid","")
            return {
            'utc': int(time.time())
        }
        _LOGGER.debug(f"heartbeat: {data}")

    async def _handle_handshake(self, data: dict):
        """处理握手包(未实现)"""
        if 'localIp' in data:
            entity_id = data['localIp'].replace('.', '_')

        _LOGGER.debug(f"handshark: {data}")

    async def async_toggle_device(self, device_id: str):
        """切换设备状态"""
        state_list = get_current_state(self.hass)
        current = get_state_by_id(self.hass, device_id)
        new_state = 1 if current == 0 else 0
        device_list = get_current_devices(self.hass)
        uid = get_uid_by_id(self.hass, device_id)
        if uid:
            await self._send_control(device_id, uid, new_state)
            set_state_by_id(self.hass, device_id, new_state)

    async def async_turn_on(self, device_id: str):
        """打开设备"""
        uid = get_uid_by_id(self.hass, device_id)
        if uid:
            result = await self._send_control(device_id, uid, 0)
            if result:
                set_state_by_id(self.hass, device_id, 0)
                return True
        return False

    async def async_turn_off(self, device_id: str):
        """关闭设备"""
        uid = get_uid_by_id(self.hass, device_id)
        if uid:
            result = await self._send_control(device_id, uid, 1)
            if result:
                set_state_by_id(self.hass, device_id, 1)
                return True
        return False

    def _update_activity(self, msg):
        """更新最后活跃时间"""
        self._last_active_time = datetime.now()
        _LOGGER.debug("%s, 激活时间：%s", msg, self._last_active_time)
