#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>
#include <Wire.h>
#include <Adafruit_GFX.h>
#include <Adafruit_SSD1306.h>
#include <Adafruit_AHTX0.h>

// ================= 配置区域 =================
const char* ssid = "apple";              
const char* password = "12345687";       
String serverBaseUrl = "http://172.20.10.5:5001"; 

// ================= 硬件引脚定义 =================
#define K230_RX_PIN 17
#define K230_TX_PIN 23
#define COMMON_TRIG_PIN 16 
#define BUZZER_PIN 25  // TODO: 蜂鸣器引脚

// I2C 引脚 
#define I2C_SDA 21
#define I2C_SCL 22

// ================= 全局对象与互斥锁 =================
Adafruit_SSD1306 display(128, 64, &Wire, -1);
Adafruit_AHTX0 aht;

// 【核心】定义 I2C 互斥锁
SemaphoreHandle_t xI2CMutex; 

const int DISTANCE_THRESHOLD = 50; 

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
  {1, 13, 32, 0, -1, 0,HIGH}, 
  {2, 14, 33, 0, -1, 0,HIGH},
  {3, 18, 35, 0, -1, 0,HIGH},
  {4, 19, 34, 0, -1, 0,HIGH}
};

// ================= 函数声明 =================
float getDistance(int echoPin);
void sendStatusToServer(int slot_id, int occupied, int charging);
void verifyPlateWithServer(String plate);
void safeOLEDPrint(String line1, String line2);
void triggerAlarm();

// FreeRTOS 任务
void TaskSensors(void *pvParameters);
void TaskK230UART(void *pvParameters);
void TaskEnvMonitor(void *pvParameters); // 环境监测任务
void TaskButtons(void *pvParameters); // 按键监测任务

void setup() {
  Serial.begin(115200);
  Serial2.begin(115200, SERIAL_8N1, K230_RX_PIN, K230_TX_PIN);

  pinMode(COMMON_TRIG_PIN, OUTPUT);
  pinMode(BUZZER_PIN, OUTPUT);
  digitalWrite(BUZZER_PIN, LOW); // 默认关闭蜂鸣器

  for (int i = 0; i < 4; i++) {
    pinMode(slots[i].echoPin, INPUT);
    pinMode(slots[i].buttonpin, INPUT_PULLUP); // 按键使用内置上拉
  }

  // 1. 初始化互斥锁 (非常重要，必须在初始化 I2C 设备前创建)
  xI2CMutex = xSemaphoreCreateMutex();

  // 2. 初始化 I2C 总线
  Wire.begin(I2C_SDA, I2C_SCL);

  // 3. 安全初始化 OLED 和 AHT20
  if (xSemaphoreTake(xI2CMutex, portMAX_DELAY)) {
    if(!display.begin(SSD1306_SWITCHCAPVCC, 0x3C)) {
      Serial.println(F("SSD1306 allocation failed"));
    } else {
      display.clearDisplay();
      display.setTextSize(1);
      display.setTextColor(WHITE);
      display.setCursor(0, 10);
      display.println("System Booting...");
      display.display();
    }
    
    if (!aht.begin()) {
      Serial.println("Could not find AHT? Check wiring");
    }
    xSemaphoreGive(xI2CMutex); // 释放锁
  }

  WiFi.begin(ssid, password);
  Serial.print("Connecting WiFi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500); Serial.print(".");
  }
  
  safeOLEDPrint("WiFi Connected!", WiFi.localIP().toString());
// 创建 FreeRTOS 任务，分别处理传感器、UART 和环境监测
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
    8192, 
    NULL, 
    2,                    // UART 任务优先级稍高，确保及时处理车牌数据
    NULL, 
    0                     // 运行在 Core 0，和 Serial 输出在同一核心，避免冲突
  );
  // 温湿度任务
  xTaskCreatePinnedToCore(TaskEnvMonitor, "EnvTask", 4096, NULL, 1, NULL, 1); 
  // 按键任务
  xTaskCreatePinnedToCore(TaskButtons, "ButtonTask", 4096, NULL, 2, NULL, 1); 
  
}

void loop() {
  vTaskDelay(1000); 
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
            Serial.printf("[Button] Slot %d Charging state: %d\n", slots[i].id, slots[i].isCharging);
            
            safeOLEDPrint("Slot " + String(slots[i].id), slots[i].isCharging ? "Charging ON" : "Charging OFF");
            
            // 立即上报最新状态到服务器
            sendStatusToServer(slots[i].id, slots[i].isOccupied, slots[i].isCharging);
        } else {
            // 没车按充电没反应，并提示
            Serial.printf("[Button] Slot %d is empty! Cannot charge.\n", slots[i].id);
            safeOLEDPrint("Slot " + String(slots[i].id), "Empty! Cannot Charge");
            triggerAlarm(); // 滴一声提示操作无效
        }
      }
      slots[i].lastButtonState = reading; // 记录当前状态，供下次比较
    }
    
    // 按键防抖延时 (非常关键，防止按一下触发好几次)
    vTaskDelay(pdMS_TO_TICKS(50)); 
  }
}

// ================= I2C 线程安全显示函数 =================
void safeOLEDPrint(String line1, String line2) {
  // 请求互斥锁，最多等待 portMAX_DELAY (死等)
  if (xSemaphoreTake(xI2CMutex, portMAX_DELAY)) {
    display.clearDisplay();
    display.setTextSize(2);      // 字体稍微大点
    display.setCursor(0, 0);
    display.println(line1);
    
    display.setTextSize(1);
    display.setCursor(0, 30);
    display.println(line2);
    
    display.display();
    
    // 操作完毕，必须释放锁！否则其他任务会死锁
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
      float distance = getDistance(slots[i].echoPin);
      slots[i].isOccupied = (distance > 0 && distance < DISTANCE_THRESHOLD) ? 1 : 0;

      if (slots[i].isOccupied != slots[i].lastReportedState) {
        sendStatusToServer(slots[i].id, slots[i].isOccupied);
        slots[i].lastReportedState = slots[i].isOccupied;
      }
      
      vTaskDelay(pdMS_TO_TICKS(60)); // 每个车位间隔 60ms，4 个车位总共 240ms，留点余量每 300ms 检测一次
    }
    vTaskDelay(pdMS_TO_TICKS(1000));
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
        if (incomingPlate.length() > 0) {
          safeOLEDPrint("Checking:", incomingPlate);
          verifyPlateWithServer(incomingPlate);
        }
        incomingPlate = "";
      } else {
        incomingPlate += c;
      }
    }
    vTaskDelay(pdMS_TO_TICKS(20));
  }
}

// ================= 任务 3：读取 AHT20 并上报 =================
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

// ================= 底层功能函数 =================

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
        safeOLEDPrint("WELCOME!", plate + "\nSlot: " + slotNumber);
      } else if (response == "FAIL") {
        safeOLEDPrint("DENIED", "Please\nReserve First!");
        triggerAlarm(); // 未预约在门口逗留，也给个报警提示
      }
    }
    http.end();
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
       // 【核心逻辑】：解析后端传回的 ALARM 指令
       if(response == "ALARM") {
          Serial.printf(">>> [ALARM] Slot %d 违停！\n", slot_id);
          safeOLEDPrint("WARNING!", "Slot " + String(slot_id) + "\nUnreserved!");
          triggerAlarm(); // 触发物理蜂鸣器
       }
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
  
  long duration = pulseIn(echoPin, HIGH, 30000); 
  if (duration == 0) return 999.0; 
  return duration * 0.034 / 2;
}