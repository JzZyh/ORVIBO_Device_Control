
from .const import (
    DOMAIN,
    ORVIBO_SWITCH_MODEL
)

def get_data_from_list(data: list[dict], key1: str, value1, key2: str, def_value):
    import logging
    _LOGGER = logging.getLogger(__name__)
    try:
        _LOGGER.debug("get_data_from_list调用: key1=%s, value1=%s, key2=%s, def_value=%s", key1, value1, key2, def_value)
        _LOGGER.debug("数据列表: %s", data)
        if not isinstance(data, list):
            _LOGGER.error("数据不是列表类型: %s", type(data))
            return def_value
        
        for device in data:
            _LOGGER.debug("检查设备: %s", device)
            if device.get(key1) == value1:
                _LOGGER.debug("找到匹配项: %s=%s, 返回%s=%s", key1, value1, key2, device.get(key2, def_value))
                return device.get(key2, def_value)
        
        _LOGGER.debug("未找到匹配项，返回默认值: %s", def_value)
        return def_value
    except Exception as e:
        _LOGGER.error("get_data_from_list错误: %s", e)
        return def_value

def set_data_in_list(data: list[dict], key1: str, value1, key2: str, def_value)->bool:
    for device in data:
        if device.get(key1) == value1:
            device[key2] = def_value
            return True
    return False

def deduplicate_by_key(data: list[dict], key: str):
    result_dict = {}
    for item in data:
        device_id = item.get(key)
        if device_id is not None:
            # 如果设备已经存在，优先保留delFlag=0的设备
            if device_id in result_dict:
                existing_item = result_dict[device_id]
                # 如果现有设备是delFlag=1而新设备是delFlag=0，则替换
                if existing_item.get('delFlag') == 1 and item.get('delFlag') != 1:
                    result_dict[device_id] = item
            else:
                result_dict[device_id] = item
    return list(result_dict.values())

def get_current_floors(hass):
    return hass.data[DOMAIN]["floor"]

def get_current_family(hass):
    return hass.data[DOMAIN]["family"]

def get_current_rooms(hass):
    return hass.data[DOMAIN]["room_list"]

def get_current_devices(hass):
    return hass.data[DOMAIN]["device_list"]

def get_current_state(hass):
    return hass.data[DOMAIN]["state_list"]

def get_name_by_id(hass, device_id):
    return get_data_from_list(hass.data[DOMAIN]["device_list"], "deviceId", device_id, "deviceName", "")

def get_uid_by_id(hass, device_id):
    return get_data_from_list(hass.data[DOMAIN]["device_list"], "deviceId", device_id, "uid", "")

def get_model_by_id(hass, device_id):
    return get_data_from_list(hass.data[DOMAIN]["device_list"], "deviceId", device_id, "model", "")

def get_room_id_by_id(hass, device_id):
    return get_data_from_list(hass.data[DOMAIN]["device_list"], "deviceId", device_id, "roomId", "")

def get_name_by_uid(hass, uid):
    return get_data_from_list(hass.data[DOMAIN]["device_list"], "uid", uid, "deviceName", "")

def get_id_by_uid(hass, uid):
    import logging
    _LOGGER = logging.getLogger(__name__)
    device_list = hass.data[DOMAIN]["device_list"]
    _LOGGER.debug("get_id_by_uid调用: UID=%s, 设备列表=%s", uid, device_list)
    
    # 遍历设备列表，查找匹配的UID
    for device in device_list:
        device_uid = device.get("uid")
        device_id = device.get("deviceId")
        _LOGGER.debug("检查设备: UID=%s, device_id=%s, 名称=%s", device_uid, device_id, device.get('deviceName'))
        if device_uid == uid:
            _LOGGER.debug("找到匹配的设备: UID=%s, device_id=%s, 名称=%s", device_uid, device_id, device.get('deviceName'))
            return device_id
    
    _LOGGER.debug("未找到匹配的设备，UID=%s", uid)
    return ""

def get_state_by_id(hass, device_id):
    return get_data_from_list(hass.data[DOMAIN]["state_list"], "deviceId", device_id, "value1", 1)

def get_model_name_by_model_id(hass, model_id):
    return ORVIBO_SWITCH_MODEL.get(model_id,"")

def get_room_name_by_room_id(hass, room_id):
    return get_data_from_list(hass.data[DOMAIN]["room_list"], "roomId", room_id, "roomName", "")

def set_state_by_id(hass, device_id, state):
    return set_data_in_list(hass.data[DOMAIN]["state_list"], "deviceId", device_id, "value1", state)

def set_state_by_uid(hass, uid, state):
    return set_data_in_list(hass.data[DOMAIN]["state_list"], "uid", uid, "value1", state)

def set_current_floor(hass, floor):
    hass.data[DOMAIN]["floor"] = floor

def set_current_family(hass, family):
    hass.data[DOMAIN]["family"] = family

def set_current_rooms(hass, rooms):
    hass.data[DOMAIN]["room_list"] = rooms

def set_current_devices(hass, devices):
    hass.data[DOMAIN]["device_list"] = devices

def set_current_state(hass, state_list):
    hass.data[DOMAIN]["state_list"] = state_list

def set_device_state(hass, device_id, state):
    set_data_in_list(hass.data[DOMAIN]["state_list"], "deviceId", device_id, "state", state)