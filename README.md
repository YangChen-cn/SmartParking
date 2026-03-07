# 🚗 Smart Parking & EV Charging System｜智慧停车与电动汽车充电系统

一个集智能车位管理、电动汽车充电管理、实时环境监测于一体的全栈解决方案。本系统通过物联网技术实现停车位的自动检测、预约管理和充电桩的intelligent控制。

## 📋 项目概述 | Project Overview

**SmartParking** 是一个现代化的智慧停车管理系统，适用于校园、商业中心等场景。该系统集成了以下核心功能：

- 🅿️ **智能车位检测**：采用超声波传感器实时检测车位占用状态
- 🔌 **EV充电管理**：完整的充电站管理和计费功能  
- 📷 **车牌识别系统**：基于K230的实时车牌识别和抓拍
- 🌡️ **环境监测**：实时温湿度显示和历史数据记录
- 💻 **网页管理界面**：直观的实时监控和数据可视化
- 📱 **REST API**：为未来的移动应用提供接口支持

## 🏗️ 系统架构 | System Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Web Dashboard                        │
│     (Flask后端 + Bootstrap前端 + Chart.js数据可视化)    │
└────────────────────────┬────────────────────────────────┘
                         │ HTTP/REST API
        ┌────────────────┴────────────────┐
        │                                  │
   ┌────▼──────┐                    ┌────▼──────┐
   │  数据库    │                    │  静态资源   │
   │ (SQLite)  │                    │   & API   │
   └───────────┘                    └───────────┘
        │                                  │
        └────────────┬─────────────────────┘
                     │
        ┌────────────▼────────────┐
        │   ESP32 主控单元        │
        │  (固件 - C++/Arduino)   │
        └─┬──────────┬──────────┬─┘
          │          │          │
    ┌─────▼──┐  ┌───▼────┐  ┌─▼──────┐
    │ K230   │  │ 传感器  │  │ 执行   │
    │车牌识别 │  │ 阵列   │  │机制   │
    │        │  │        │  │       │
    └────────┘  └────┬───┘  └───────┘
                     │
         ┌───────────┼────────────┐
         │           │            │
    ┌────▼──┐  ┌────▼───┐  ┌────▼──┐
    │ 超声波 │  │温湿度  │  │OLED   │
    │传感器  │  │传感器  │  │显示屏  │
    └────────┘  └────────┘  └───────┘
```

## 🛠️ 技术栈 | Tech Stack

| 层级 | 技术 | 说明 |
|------|------|------|
| **后端** | Python Flask | Web框架，RESTful API服务 |
| **数据库** | SQLite | 轻量级关系型数据库 |
| **前端** | Bootstrap 5 | 响应式UI框架 |
| **数据可视化** | Chart.js | 实时数据图表 |
| **嵌入式** | C++ / Arduino | ESP32固件开发 |
| **实时操作系统** | FreeRTOS | 多任务并发调度 |
| **视觉识别** | K230 SDK | 车牌识别模块 |
| **硬件** | ESP32, 超声波, AHT20/AHT21 | 传感器和控制器 |
| **通信** | HTTP, WiFi | 物联网连接 |


## 📁 项目结构 | Project Structure

```
SmartParking/
├── app.py                       # Flask主应用程序
├── parking.db                   # SQLite数据库
├── firmware/                    # ESP32固件代码
│   ├── platformio.ini           # PlatformIO配置
│   └── src/
│       └── main.cpp             # ESP32主程序（车位检测、传感器、WiFi通信）
├── k230/                        # K230车牌识别模块
│   └── main.py                  # 车牌识别算法
├── templates/                   # 前端模板
│   └── index.html               # 网页管理界面
├── static/
│   └── snapshots/               # 车牌抓拍相册
├── .venv/                       # Python虚拟环境
└── README.md                    # 本文件
```

## 💾 数据库设计 | Database Schema

### 车位状态表 (slots)
```sql
CREATE TABLE slots (
    id INTEGER PRIMARY KEY,              -- 车位编号
    occupied INTEGER,                    -- 占用状态 (0/1)
    charging INTEGER,                    -- 充电状态 (0/1)
    license_plate TEXT,                  -- 车牌号
    start_time TEXT,                     -- 停车开始时间
    end_time TEXT,                       -- 停车结束时间
    password TEXT                        -- 预约密码
);
```

### 充电日志表 (charging_logs)
```sql
CREATE TABLE charging_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slot_id INTEGER,                     -- 关联的车位ID
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
);
```

### 抓拍记录表 (snapshot_logs)
```sql
CREATE TABLE snapshot_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    image_url TEXT,                      -- 图片存储路径
    timestamp TEXT                       -- 抓拍时间
);
```

## 🔧 硬件配置 | Hardware Configuration

### ESP32 引脚定义
| 功能 | GPIO引脚 | 备注 |
|------|---------|------|
| K230 RX | GPIO 17 | UART接收 |
| K230 TX | GPIO 23 | UART发送 |
| 超声波触发 | GPIO 16 | 配置为触发多个超声波传感器 |
| 蜂鸣器 | GPIO 25 | 报警信号 |
| I2C SDA | GPIO 21 | 温湿度传感器和OLED屏 |
| I2C SCL | GPIO 22 | 温湿度传感器和OLED屏 |

### 外接硬件
- **传感器**：Adafruit AHTX0 温湿度传感器 (I2C)
- **显示屏**：SSD1306 OLED屏 128×64 (I2C)
- **识别模块**：K230 嘉楠芯片 (UART)
- **超声波**：多个HC-SR04/兼容传感器（GPIO驱动）

## ⚙️ FreeRTOS 多任务设计 | FreeRTOS Multi-tasking

本项目充分利用 FreeRTOS 实时操作系统实现多任务并发执行，确保各功能模块高效协作：

### 核心任务架构

| 任务名 | 优先级 | 功能描述 |
|-------|--------|---------|
| **WiFi通信任务** | 中 | 处理HTTP请求、API调用、与服务器同步 |
| **传感器采集任务** | 高 | 读取I2C传感器（温湿度、超声波）数据 |
| **车牌识别任务** | 高 | 接收K230识别结果、处理UART数据 |
| **显示任务** | 低 | 更新OLED屏幕显示内容 |
| **数据处理任务** | 中 | 数据验证、异常处理、状态判断 |

### 互斥锁与同步机制

- **I2C互斥锁 (xI2CMutex)**：保护I2C总线访问，防止多任务同时操作I2C导致数据冲突
- **队列 (xPlateQueue)**：K230车牌数据的任务间通信，采用消息队列模式解耦任务

### 任务优先级分配原则

- **高优先级**：时间敏感的传感器采集任务，确保实时性和精确度
- **中优先级**：网络通信和数据处理，平衡响应速度和系统整体流畅度
- **低优先级**：UI显示任务，不影响核心功能


## 🚀 快速开始 | Quick Start

### 前置条件
- Python 3.8+
- PlatformIO (用于ESP32固件烧录)
- WiFi网络连接

### 1. 后端部署 | Backend Setup

```bash
# 进入项目目录
cd /Users/yang/Desktop/SmartParking

# 创建虚拟环境（如尚未创建）
python3 -m venv .venv

# 激活虚拟环境
source .venv/bin/activate

# 安装依赖
pip install flask

# 运行应用
python app.py
```

应用将在 `http://localhost:5000` 启动

### 2. 固件烧录 | Firmware Upload

```bash
# 进入固件目录
cd firmware

# 连接ESP32，使用PlatformIO烧录
pio run -t upload

# 监听串口输出
pio device monitor --baud 115200
```

### 3. 配置WiFi和服务器地址

在 `firmware/src/main.cpp` 中修改以下配置：

```cpp
const char* ssid = "your_wifi_ssid";              
const char* password = "your_wifi_password";       
String serverBaseUrl = "http://your_server_ip:5001"; 
```

### 4. 访问网页界面 | Access Web Dashboard

打开浏览器访问：`http://localhost:5000/`

## 📡 API 文档 | API Documentation

### 获取车位状态
```
GET /api/status
返回：
[
  {
    "id": 1,
    "occupied": false,
    "charging": false,
    "license_plate": null,
    "start_time": null,
    "end_time": null
  },
  ...
]
```

### 上传抓拍图片
```
POST /upload_frame
Content-Type: application/octet-stream
Body: 二进制图片数据

响应：Image Saved OK (HTTP 200)
```

### 获取抓拍相册
```
GET /api/snapshots
返回最新20张抓拍记录：
[
  {
    "url": "/static/snapshots/20260307_163000.jpg",
    "time": "2026-03-07 16:30:00"
  },
  ...
]
```

### 更新环境数据
```
GET /update_env?temp=28.5&hum=65
```

### 获取实时环境数据
```
GET /api/env
返回：
{
  "temp": "28.5",
  "hum": "65"
}
```

## ✨ 核心功能 | Key Features

### 1️⃣ 实时车位监控
- 每个车位配备超声波传感器
- 距离阈值自动判断占用状态（默认阈值：20cm）
- 实时推送状态变化

### 2️⃣ 智能充电管理
- 独立的充电状态标志
- 自动充电日志记录
- 支持多车位同时充电

### 3️⃣ 车牌识别与抓拍
- K230芯片实时车牌识别（H264编码）
- 进出时自动抓拍
- 历史记录可视化相册

### 4️⃣ 环境监测
- 温湿度实时显示
- OLED屏幕显示参数
- 数据持久化存储

### 5️⃣ 预约管理
- 支持车位预约（start_time / end_time）
- 自动超时取消（5分钟无车位变化）
- 一车一密（password字段）

## 📊 工作流程 | Workflow

```
┌─────────────┐
│   车辆进入   │
└──────┬──────┘
       │
       ▼
┌──────────────────┐
│ K230识别车牌     │
│ 抓拍存储图片     │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ ESP32检测车位    │
│ 更新数据库       │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│ 网页端显示状态   │
│ 计费算法运行     │
└────────┬─────────┘
         │
         ▼
┌──────────────────┐
│   车辆离开       │
│ 记录停留时间     │
└──────────────────┘
```

## 🐛 故障排除 | Troubleshooting

| 问题 | 解决方案 |
|------|---------|
| ESP32无法连接WiFi | 检查SSID、密码和WiFi信号强度 |
| 传感器无响应 | 检查I2C总线和地址（SSD1306默认0x3C, AHTX0默认0x38） |
| 数据库错误 | 删除 `parking.db` 重新初始化 |
| 网络连接超时 | 确认服务器IP和端口正确 |
| 抓拍图片存储失败 | 检查 `/static/snapshots/` 目录权限和磁盘空间 |


## 🎯 未来改进 | Future Enhancements

- [ ] 移动端App（iOS/Android）
- [ ] 支付集成（支付宝/微信）
- [ ] 大数据分析与预测模型
- [ ] 多楼层停车场支持
- [ ] 车主App推送通知
- [ ] 能耗统计和优化
- [ ] 车位预留规则引擎
- [ ] 实时视频监控流

## 👥 团队成员 | Team

- 项目设计与开发：Yang

**最后更新**：2026年3月 | Last Updated: March 2026
