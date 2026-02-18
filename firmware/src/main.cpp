#include <Arduino.h>
#include <WiFi.h>
#include <HTTPClient.h>

// ================= 配置区域 (请修改这里) =================
// 1. 你的 Wi-Fi 名字和密码 (必须和电脑连在同一个 Wi-Fi 下)
const char* ssid = "apple";     // TODO: 改成你的 Wi-Fi 名字
const char* password = "12345687"; // TODO: 改成你的 Wi-Fi 密码

// 2. 你的电脑 IP 地址 (在电脑 CMD 输入 ipconfig 查看)
// 格式必须是: http://IP地址:5001/update
String serverUrl = "http://172.20.10.5:5001/update"; // TODO: 改成你的电脑 IP

// ================= 硬件引脚定义 =================
#define TRIG_PIN 26  // 发射脚
#define ECHO_PIN 27  // 接收脚

// 定义车位状态阈值 (厘米)
// 如果距离小于 50cm，认为有车
const int DISTANCE_THRESHOLD = 50; 
// 模拟的车位 ID
const int SLOT_ID = 1; 

void setup() {
  // 1. 初始化串口通信
  Serial.begin(115200);
  
  // 2. 配置超声波引脚
  pinMode(TRIG_PIN, OUTPUT);
  pinMode(ECHO_PIN, INPUT);

  // 3. 连接 Wi-Fi
  WiFi.begin(ssid, password);
  Serial.print("Connecting to Wi-Fi");
  while (WiFi.status() != WL_CONNECTED) {
    delay(500);
    Serial.print(".");
  }
  Serial.println("");
  Serial.print("Connected! IP Address: ");
  Serial.println(WiFi.localIP());
}

float getDistance() {
  // 清空 Trig 引脚
  digitalWrite(TRIG_PIN, LOW);
  delayMicroseconds(2);
  
  // 发送 10 微秒的高电平脉冲
  digitalWrite(TRIG_PIN, HIGH);
  delayMicroseconds(10);
  digitalWrite(TRIG_PIN, LOW);
  
  // 读取 Echo 引脚的高电平持续时间 (微秒)
  long duration = pulseIn(ECHO_PIN, HIGH);
  
  // 计算距离: 距离 = 时间 * 声速(0.034 cm/us) / 2
  float distance = duration * 0.034 / 2;
  return distance;
}

void loop() {
  // 1. 获取距离
  float distance = getDistance();
  Serial.print("Distance: ");
  Serial.print(distance);
  Serial.println(" cm");

  // 2. 判断车位状态 (有车=1, 没车=0)
  int occupied = (distance > 0 && distance < DISTANCE_THRESHOLD) ? 1 : 0;
  
  // 3. 发送数据给 Python 后端
  if(WiFi.status() == WL_CONNECTED) {
    HTTPClient http;
    
    // 拼接完整的 URL，例如: http://192.168.1.5:5001/update?id=1&occupied=1&charging=0
    String url = serverUrl + "?id=" + String(SLOT_ID) + 
                 "&occupied=" + String(occupied) + 
                 "&charging=0"; // 暂时默认不充电
    
    Serial.print("Sending Request: ");
    Serial.println(url);
    
    http.begin(url); // 启动连接
    int httpCode = http.GET(); // 发送 GET 请求
    
    if (httpCode > 0) {
      String payload = http.getString();
      Serial.println("Server Response: " + payload);
    } else {
      Serial.print("Error on HTTP request: ");
      Serial.println(httpCode);
    }
    http.end(); // 释放资源
  } else {
    Serial.println("WiFi Disconnected");
  }

  // 4. 每 2 秒检测一次 (不要太快，给服务器喘息时间)
  delay(2000);
}