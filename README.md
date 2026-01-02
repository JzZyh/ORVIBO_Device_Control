# ORVIBO_Device_Control

欧瑞博设备控制 Home Assistant 自定义组件

## 支持的设备型号

| 设备型号 | 设备类型 | 备注 |
|---------|---------|------|
| 56d124ba95474fc98aafdb830e933789 | Switch | S20c |
| 04aa419575be4714a853a82be3f22035 | Switch | S30c |
| f3be30b8c43c44da85aac622e5b56111 | Switch | |
| 71a0b275d9ba4895afdaf400bc7e3a0d | Switch | |
| b7313321dbe74da384d136a2a3fa2005 | Switch | |
| 2e13af8e17434961be98f055d68c2166 | Switch | |
| f5f2d6e6f4a14a82bee85032c27dbd1e | Air Conditioner | 空调 |
| 396483ce8b3f4e0d8e9d79079a35a420 | Ventilation | 通风设备 |

## 使用方法

1. 将本组件放入 Home Assistant 的 `custom_components` 目录
2. 重启 Home Assistant
3. 在 Home Assistant 界面中添加集成
4. 输入欧瑞博账号和密码完成配置

## 功能

- 支持开关设备控制
- 支持空调设备控制
- 支持风扇设备控制
- 实时更新设备状态