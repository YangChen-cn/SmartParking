#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_AHTX0.h>
#include <Servo.h>             

// ================= Serial 打印控制宏 =================
#define ENABLE_DEBUG_SERIAL 1   // 1 启用 Serial 打印, 0 关闭所有打印

#if ENABLE_DEBUG_SERIAL
  #define DEBUG_PRINT(x) Serial.print(x)
  #define DEBUG_PRINTLN(x) Serial.println(x)
  #define DEBUG_PRINTF(...) Serial.printf(__VA_ARGS__)
#else
  #define DEBUG_PRINT(x)
  #define DEBUG_PRINTLN(x)
  #define DEBUG_PRINTF(...)
#endif

// ================= 配置区域 =================
const char* ssid = "apple";              
const char* password = "12345687";       
String serverBaseUrl = "http://api.campusparking.xyz";

// ================= 硬件引脚定义 =================
#define K230_RX_PIN 17
#define K230_TX_PIN 23
#define COMMON_TRIG_PIN 16 
#define BUZZER_PIN 25  // TODO: 蜂鸣器引脚
#define SERVO_PIN  4  // 舵机引脚

// I2C 引脚 
#define I2C_SDA 21
#define I2C_SCL 22

// ================= 全局对象与互斥锁 =================
Adafruit_SSD1306 display(128, 64, &Wire, -1);
Adafruit_AHTX0 aht;
Servo gateServo;

// 【核心】定义 I2C 互斥锁
SemaphoreHandle_t xI2CMutex; 

//车牌消息结构体
struct PlateMessage {
  char number[16]; 
};

//队列句柄
QueueHandle_t xPlateQueue;

// ================= 警告状态追踪 =================

// ================= 超声波检测参数优化 =================
// 迟滞阈值（单位: cm）：占用判定 < DISTANCE_OCCUPIED，空闲判定 > DISTANCE_EMPTY
const int DISTANCE_OCCUPIED = 6;  // 认为被占用的距离阈值（更敏感）
const int DISTANCE_EMPTY = 18;     // 认为空闲的距离阈值（防止抖动）
const int DISTANCE_MIN = 1;        // 最小有效距离（过近排除）
const int DISTANCE_MAX = 100;      // 最大有效距离（超声波范围限制）
const int SAMPLE_COUNT = 5;        // 采样次数
const int VALID_SAMPLE_THRESHOLD = 4; // 至少需要4个有效样本
const int SAMPLE_INTERVAL = 8;     // 采样间隔(ms)
const long PULSE_TIMEOUT_US = 12000; // pulseIn超时12ms，减少无回波时的阻塞

struct ParkingSlot {
  int id;
  int echoPin;
  int buttonpin;
  int isOccupied;        
  int lastReportedState; 
  int isCharging;        // 充电状态 (0闲置, 1充电)
  int lastButtonState;   // 记录按键上次的电平 (用于检测按下瞬间)
};

ParkingSlot slots[4] = {
  {1, 13, 32, 0, -1, 0, HIGH}, 
  {2, 14, 33, 0, -1, 0, HIGH},
  {3, 18, 35, 0, -1, 0, HIGH},
  {4, 19, 34, 0, -1, 0, HIGH},
};

// ================= 函数声明 =================
float getDistance(int echoPin);
void sendStatusToServer(int slot_id, int occupied, int charging);
void verifyPlateWithServer(String plate);
void safeOLEDPrint(String line1, String line2);
void safeOLEDPrintSlot(int slotId, String status, String detail);
void safeOLEDPrintAlert(String title, String message, bool isSuccess);
void showMainScreen();
void drawCenteredText(String text, int y, int textSize);
void triggerAlarm();
int medianFilter(int arr[], int size);

// FreeRTOS 任务
void TaskSensors(void *pvParameters);
void TaskK230UART(void *pvParameters);
void TaskPlateVerify(void *pvParameters);
void TaskEnvMonitor(void *pvParameters); // 环境监测任务
void TaskButtons(void *pvParameters); // 按键监测任务

void setup() {
  Serial.begin(115200);
  Serial2.begin(115200, SERIAL_8N1, K230_RX_PIN, K230_TX_PIN);

  pinMode(COMMON_TRIG_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW); // 默认关闭蜂鸣器
  pinMode(SERVO_PIN, OUTPUT);
  gateServo.attach(SERVO_PIN);
  gateServo.write(0);           // 0° = 关

  for (int i = 0; i < 4; i++) {
    pinMode(slots[i].echoPin, INPUT);
    pinMode(slots[i].buttonpin, INPUT_PULLUP); // 按键使用内置上拉
  }

  // 1. 初始化互斥锁 
  xI2CMutex = xSemaphoreCreateMutex();

  // 2. 初始化 I2C 总线
  Wire.begin(I2C_SDA, I2C_SCL);

  // 3. 安全初始化 OLED 和 AHT20
  if (xSemaphoreTake(xI2CMutex, portMAX_DELAY)) {
    if(!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
      DEBUG_PRINTLN(F("SSD1306 allocation failed"));
    } else {
      display.clearDisplay();
      display.setTextSize(1);
      display.setTextColor(WHITE);
      display.setCursor(0, 10);
      display.println("System Booting...");
      display.display();
    }
    
    if (!aht.begin()) {
      DEBUG_PRINTLN("Could not find AHT? Check wiring");
    }
    xSemaphoreGive(xI2CMutex); // 释放锁
  }

  WiFi.begin(ssid, password);
  DEBUG_PRINT("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(100); DEBUG_PRINT(".");
  }
  
  safeOLEDPrint("WiFi OK", WiFi.localIP().toString());
  vTaskDelay(pdMS_TO_TICKS(500));
  showMainScreen();

// 创建队列：深度为5，每个成员大小为 PlateMessage 结构体
  xPlateQueue = xQueueCreate(5, sizeof(struct PlateMessage));

  if (xPlateQueue == NULL) {
    DEBUG_PRINTLN("Queue creation failed!");
  }
// 创建 FreeRTOS 任务，分别处理传感器、UART 、按键和环境监测
xTaskCreatePinnedToCore(
    TaskSensors,    // 任务执行的函数
    "SensorTask",      // 任务名称
    8192,                 // 栈空间大小 (HTTP请求很耗内存，给大点)
    NULL,                 // 传递给任务的参数
    1,                    // 任务优先级 (1比较低)
    NULL,                 // 任务句柄
    1                     // 指定运行在 Core 1
  );
  xTaskCreatePinnedToCore(
    TaskK230UART, 
    "UartTask", 
    4096, 
    NULL, 
    2,                    // UART 任务优先级稍高，确保及时处理车牌数据
    NULL, 
    0                     // 运行在 Core 0，和 Serial 输出在同一核心，避免冲突
  );
  // 专门负责核对车牌的任务，给 8192 栈空间处理 HTTP
  xTaskCreatePinnedToCore(TaskPlateVerify, "VerifyTask", 8192, NULL, 1, NULL, 0);
  // 温湿度任务
  xTaskCreatePinnedToCore(TaskEnvMonitor, "EnvTask", 4096, NULL, 1, NULL, 1); 
  // 按键任务
  xTaskCreatePinnedToCore(TaskButtons, "ButtonTask", 4096, NULL, 2, NULL, 1); 
  
}

void loop() {
  // 主循环不执行任何操作，所有逻辑都在 FreeRTOS 任务中处理
}

// ================= I2C 线程安全显示函数 =================

// 中心对齐长文本
void drawCenteredText(String text, int y, int textSize) {
  display.setTextSize(textSize);
  int16_t x1, y1;
  uint16_t w, h;
  display.getTextBounds(text, 0, y, &x1, &y1, &w, &h);
  display.setCursor((128 - w) / 2, y);
  display.println(text);
}

// 主界面：显示停车场名称
void showMainScreen() {
  if (xSemaphoreTake(xI2CMutex, portMAX_DELAY)) {
    display.clearDisplay();
    display.setTextColor(WHITE);
    
    // 顶部装饰线
    display.drawLine(0, 0, 128, 0, WHITE);
    
    // 标题
    display.setTextSize(1);
    display.setCursor(8, 4);
    display.println("PARKING SYSTEM");
    
    // 中间分隔线
    display.drawLine(0, 15, 128, 15, WHITE);
    
    // 停车场名称（英文）
    display.setTextSize(1);
    display.setCursor(0, 22);
    display.println("Weihai Campus");
    display.setCursor(0, 32);
    display.println("BJTU");
    display.setCursor(0, 42);
    display.println("Parking Lot");
    
    // 底部分隔线
    display.drawLine(0, 52, 128, 52, WHITE);
    
    // 状态指示
    display.setTextSize(1);
    display.setCursor(35, 56);
    display.println("READY");
    
    display.display();
    xSemaphoreGive(xI2CMutex);
  }
}

// 标准两行显示（保持兼容）
void safeOLEDPrint(String line1, String line2) {
  if (xSemaphoreTake(xI2CMutex, portMAX_DELAY)) {
    display.clearDisplay();
    display.setTextColor(WHITE);
    
    // 标题：大号字体，顶部
    display.setTextSize(2);
    display.setCursor(0, 0);
    display.println(line1);
    
    // 分隔线
    display.drawLine(0, 18, 128, 18, WHITE);
    
    // 副文本：小号字体
    display.setTextSize(1);
    display.setCursor(0, 25);
    display.println(line2);
    
    display.display();
    xSemaphoreGive(xI2CMutex);
  }
}

// 车位状态显示（带图标效果，显示后自动回到主界面）
void safeOLEDPrintSlot(int slotId, String status, String detail) {
  if (xSemaphoreTake(xI2CMutex, portMAX_DELAY)) {
    display.clearDisplay();
    display.setTextColor(WHITE);
    
    // 标题行
    display.setTextSize(2);
    display.setCursor(0, 0);
    display.print("[P");
    display.print(slotId);
    display.println("]");
    
    // 分隔线
    display.drawLine(0, 18, 128, 18, WHITE);
    
    // 状态：中等字体，突出显示
    display.setTextSize(1);
    display.setCursor(8, 25);
    display.println(status);
    
    // 详情信息
    display.setTextSize(1);
    display.setCursor(8, 38);
    display.println(detail);
    
    display.display();
    xSemaphoreGive(xI2CMutex);
  }

}

// 重要提示（警告/成功，显示后自动回到主界面）
void safeOLEDPrintAlert(String title, String message, bool isSuccess) {
  if (xSemaphoreTake(xI2CMutex, portMAX_DELAY)) {
    display.clearDisplay();
    display.setTextColor(WHITE);
    
    // 边框
    display.drawRect(0, 0, 128, 64, WHITE);
    display.drawRect(1, 1, 126, 62, WHITE);
    
    // 标题（中心）
    display.setTextSize(2);
    drawCenteredText(title, 5, 2);
    
    // 分隔线
    display.drawLine(5, 22, 123, 22, WHITE);
    
    // 消息内容
    display.setTextSize(1);
    display.setCursor(10, 35);
    display.println(message);
    
    // 状态指示符
    display.setTextSize(1);
    display.setCursor(45, 52);
    display.println(isSuccess ? "[SUCCESS]" : "[WARNING]");
    
    display.display();
    xSemaphoreGive(xI2CMutex);
  }

}

// ================= 蜂鸣器报警功能 =================
void triggerAlarm() {
  // 嘀-嘀-嘀 三声急促报警
  for(int i=0; i<3; i++){
    digitalWrite(BUZZER_PIN, HIGH);
    vTaskDelay(pdMS_TO_TICKS(100));
    digitalWrite(BUZZER_PIN, LOW);
    vTaskDelay(pdMS_TO_TICKS(100));
  }
}

// ================= 任务 1：超声波检测与报警处理 =================
void TaskSensors(void *pvParameters) {
  while (1) { 
    for (int i = 0; i < 4; i++) {
      // 多次采样 + 中值滤波 + 迟滞阈值
      int distances[SAMPLE_COUNT];
      int validCount = 0;
      
      // 采样 SAMPLE_COUNT 次
      for (int k = 0; k < SAMPLE_COUNT; k++) {
        float d = getDistance(slots[i].echoPin);
        
        // 过滤不合理的距离值
        if (d >= DISTANCE_MIN && d <= DISTANCE_MAX) {
          distances[validCount] = (int)d;
          validCount++;
        }
        vTaskDelay(pdMS_TO_TICKS(SAMPLE_INTERVAL));  // 采样间隔
      }
      
      // 至少需要足够的有效样本
      if ( validCount >= VALID_SAMPLE_THRESHOLD) {
        // 计算中值（排序后取中间值）
        int medianDistance = medianFilter(distances, validCount);
        
        // 迟滞判定 - 防止在阈值附近抖动
        int newOccupiedState = slots[i].isOccupied;
        if (newOccupiedState == 0 && medianDistance < DISTANCE_OCCUPIED) {
          // 从空闲 → 占用（用较小阈值）
          newOccupiedState = 1;
        } else if (newOccupiedState == 1 && medianDistance > DISTANCE_EMPTY) {
          // 从占用 → 空闲（用较大阈值）
          newOccupiedState = 0;
        }
        
        // 迟滞判定 + 直接触发
        if (newOccupiedState != slots[i].isOccupied) {
          slots[i].isOccupied = newOccupiedState;
          
          DEBUG_PRINTF("[Sensor] Slot %d -> %s (Distance: %dcm)\n", 
                      slots[i].id, 
                      newOccupiedState ? "OCCUPIED" : "EMPTY",
                      medianDistance);
        }
      
      } else if(validCount <2) {
        // 【BUG FIX】采样不足（无法检测）=> 判定为空闲（物体离得很远或超出范围）
        // 防止车快速离开后卡在"占用"状态
        if (slots[i].isOccupied == 1) {
          slots[i].isOccupied = 0;
          DEBUG_PRINTF("[Sensor] Slot %d -> EMPTY (Detection Failed - assume left)\n", slots[i].id);
        }
      }

      // 智能联动逻辑: 如果车开走了，强制关闭充电状态
      if (slots[i].isOccupied == 0 && slots[i].isCharging == 1) {
         slots[i].isCharging = 0;
         DEBUG_PRINTF("[Sensor] Car left Slot %d. Auto-stopped charging.\n", slots[i].id);
      }

      if (slots[i].isOccupied != slots[i].lastReportedState) {
        // 边沿触发--- 传入状态 ---
        sendStatusToServer(slots[i].id, slots[i].isOccupied, slots[i].isCharging);
        slots[i].lastReportedState = slots[i].isOccupied;
      }
      
      vTaskDelay(pdMS_TO_TICKS(30));  
    }
    
    vTaskDelay(pdMS_TO_TICKS(300));  // 整体扫描周期缩短到300ms
  }
}

// ================= 任务 2：接收车牌并核对 (包含OLED逻辑) =================
void TaskK230UART(void *pvParameters) {
  String incomingPlate = "";
  while (1) {
    while (Serial2.available()) {
      char c = Serial2.read();
      if (c == '\n') {
        incomingPlate.trim();
        if (incomingPlate.length() > 0 && incomingPlate.length() < 16) {
          // 填充结构体
          struct PlateMessage msg;
          memset(msg.number, 0, sizeof(msg.number));
          strncpy(msg.number, incomingPlate.c_str(), 15);

          // [核心] 发送给队列。如果队列满了，等待 10ms，再不行就放弃（防止死锁）
          if (xQueueSend(xPlateQueue, &msg, pdMS_TO_TICKS(10)) != pdPASS) {
            DEBUG_PRINTLN("Queue Full! Plate dropped.");
          }
        }
        incomingPlate = "";
      } else {
        incomingPlate += c;
      }
    }
    vTaskDelay(pdMS_TO_TICKS(20)); // 适当休眠，让出 CPU
  }
}
// ================= 任务 3：核对车牌并显示结果 =================
void TaskPlateVerify(void *pvParameters) {
  struct PlateMessage rcvMsg;
  while (1) {
    // [核心] 阻塞式读取：如果队列为空，任务进入 Blocked 状态，不占 CPU
    // portMAX_DELAY 表示一直等到有数据为止
    if (xQueueReceive(xPlateQueue, &rcvMsg, portMAX_DELAY)) {
      String plateStr = String(rcvMsg.number);
      
      // 在 OLED 上显示正在核对
      safeOLEDPrint("VERIFYING", "Plate Check...");
      vTaskDelay(pdMS_TO_TICKS(1500));  // 显示1.5秒
      
      // 执行耗时的网络校验
      verifyPlateWithServer(plateStr);
    }
  }
}

// ================= 任务 4：读取 AHT20 并上报 =================
void TaskEnvMonitor(void *pvParameters) {
  while (1) {
    float temp = 0.0;
    float hum = 0.0;
    
    // 安全读取 I2C 传感器
    if (xSemaphoreTake(xI2CMutex, portMAX_DELAY)) {
      sensors_event_t humidity, temp_event;
      aht.getEvent(&humidity, &temp_event);
      temp = temp_event.temperature;
      hum = humidity.relative_humidity;
      xSemaphoreGive(xI2CMutex);
    }

    // HTTP 上报温湿度
    if (WiFi.status() == WL_CONNECTED && temp > -50.0) {
      HTTPClient http;
      http.setTimeout(5000);  // 5秒超时
      // 保留一位小数
      String url = serverBaseUrl + "/update_env?temp=" + String(temp, 1) + "&hum=" + String(hum, 1);
      http.begin(url);
      http.GET();
      http.end();
    }
    
    // 每 5 秒读取上传一次
    vTaskDelay(pdMS_TO_TICKS(5000)); 
  }
}

// ================= 按键扫描与充电控制 =================
void TaskButtons(void *pvParameters) {
  while (1) {
    for (int i = 0; i < 4; i++) {
      int reading = digitalRead(slots[i].buttonpin);
      
      // 检测下降沿 (当前是LOW，上次是HIGH，说明刚刚按下了按键)
      if (reading == LOW && slots[i].lastButtonState == HIGH) {
        
        // 智能联动逻辑: 只有车位上有车时，才允许启动充电
        if (slots[i].isOccupied == 1) {
            slots[i].isCharging = !slots[i].isCharging; // 状态翻转 (0变1，1变0)
            DEBUG_PRINTF("[Button] Slot %d Charging state: %d\n", slots[i].id, slots[i].isCharging);
            
            safeOLEDPrintSlot(slots[i].id, slots[i].isCharging ? "[⚡] CHARGING" : "[⚡] IDLE", slots[i].isCharging ? "Power ON" : "Power OFF");
            vTaskDelay(pdMS_TO_TICKS(3000));  // 显示3秒后回主界面
            showMainScreen();
            
            // 立即上报最新状态到服务器
            sendStatusToServer(slots[i].id, slots[i].isOccupied, slots[i].isCharging);
        } else {
            // 没车按充电没反应，并提示
            DEBUG_PRINTF("[Button] Slot %d is empty! Cannot charge.\n", slots[i].id);
            safeOLEDPrintSlot(slots[i].id, "EMPTY", "No Vehicle");
            triggerAlarm(); // 滴一声提示操作无效（立即响，不被延时阻塞）
            vTaskDelay(pdMS_TO_TICKS(3000));  // 显示3秒后回主界面
            showMainScreen();
        }
      }
      slots[i].lastButtonState = reading; // 记录当前状态，供下次比较
    }
    
    // 按键防抖延时 (非常关键，防止按一下触发好几次)
    vTaskDelay(pdMS_TO_TICKS(50)); 
  }
}

// ================= 底层功能函数 =================

// 中值滤波函数：对数组进行排序并返回中值
int medianFilter(int arr[], int size) {
  // 简单的冒泡排序（样本数少，效率足够）
  for (int i = 0; i < size - 1; i++) {
    for (int j = 0; j < size - i - 1; j++) {
      if (arr[j] > arr[j + 1]) {
        int temp = arr[j];
        arr[j] = arr[j + 1];
        arr[j + 1] = temp;
      }
    }
  }
  return arr[size / 2];
}

void verifyPlateWithServer(String plate) {
    if (WiFi.status() == WL_CONNECTED) {
        HTTPClient http;
        http.begin(serverBaseUrl + "/api/verify");
        http.addHeader("Content-Type", "text/plain; charset=utf-8");
        int httpCode = http.POST(plate);

        if (httpCode > 0) {
            String response = http.getString();
            if (response.startsWith("OK")) {
                String slotNumber = response.substring(3);
                safeOLEDPrintAlert("WELCOME!", "Slot: " + slotNumber, true);
                vTaskDelay(pdMS_TO_TICKS(2000));  // 显示2秒
                
                // **servo logic**
                gateServo.write(90);              // 打开舵机（角度根据安装调整）
                vTaskDelay(pdMS_TO_TICKS(5000));  // 保持 5 秒
                gateServo.write(0);               // 关闭舵机
                
                showMainScreen();  // 回到主界面
            } else if (response == "FAIL") {
                safeOLEDPrintAlert("DENIED", "Please Reserve", false);
                triggerAlarm();  // 立即响铃，不被阻塞
                vTaskDelay(pdMS_TO_TICKS(3000));  // 显示3秒
                showMainScreen();  // 回到主界面
            } else {
                safeOLEDPrintAlert("ERROR", "Unknown Response", false);
                vTaskDelay(pdMS_TO_TICKS(3000));  // 显示3秒
                showMainScreen();  // 回到主界面
            }
        } else {
            safeOLEDPrintAlert("ERROR", "Network Failed", false);
            vTaskDelay(pdMS_TO_TICKS(3000));
            showMainScreen();
        }
        http.end();
    } else {
        safeOLEDPrintAlert("ERROR", "WiFi Offline", false);
        vTaskDelay(pdMS_TO_TICKS(3000));
        showMainScreen();
    }
}

void sendStatusToServer(int slot_id, int occupied, int charging) {
  if (WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    String url = serverBaseUrl + "/update?id=" + String(slot_id) + "&occupied=" + String(occupied) + "&charging=" + String(charging);
    http.begin(url);
    int httpCode = http.GET();
    if(httpCode > 0) {
       String response = http.getString();
       (void)response;
    }
    http.end();
  }
}

float getDistance(int echoPin) {
  digitalWrite(COMMON_TRIG_PIN, LOW);
  delayMicroseconds(2);
  digitalWrite(COMMON_TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(COMMON_TRIG_PIN, LOW);
  
  // 超时设为12ms，避免无回波时长时间阻塞整个扫描循环
  long duration = pulseIn(echoPin, HIGH, PULSE_TIMEOUT_US);
  
  // 信号获取失败（可能是干扰或传感器问题）
  if (duration == 0) {
    return 999.0;  // 返回无效距离
  }
  
  // 距离计算: duration(us) * 343(声速m/s) / 2 / 10000
  float distance = duration * 0.034 / 2;
  
  return distance;
}
