from libs.PipeLine import PipeLine
from libs.AIBase import AIBase
from libs.AI2D import Ai2d
from libs.Utils import *
import os,sys,ujson,gc,math
from media.media import *
import nncase_runtime as nn
import ulab.numpy as np
import image
import aidemo

# --- 导入串口、时间、网络、多线程、底层socket模块 ---
from machine import UART
from machine import FPIOA
import time
import network
import _thread
import socket

# ============================================================================
# 手写 Socket HTTP 上传任务
# ============================================================================
is_uploading = False

def upload_task(jpg_data, host, port):
    """后台异步上传任务 (使用原生 socket 实现 HTTP POST)"""
    global is_uploading
    s = None
    try:
        # 1. 获取服务器地址信息
        ai = socket.getaddrinfo(host, port)
        addr = ai[0][-1]

        # 2. 创建 Socket 并连接
        s = socket.socket()
        s.connect(addr)

        # 3. 构造 HTTP POST 请求头 (修复了 f-string 语法错误，改用 .format)
        http_request_header = (
            "POST /upload_frame HTTP/1.1\r\n"
            "Host: {}:{}\r\n"
            "Content-Type: application/octet-stream\r\n"
            "Content-Length: {}\r\n"
            "Connection: close\r\n\r\n"
        ).format(host, port, len(jpg_data))

        # 4. 发送请求头 (字符串转字节流)
        s.send(http_request_header.encode('utf-8'))

        # 5. 发送图片实体数据
        s.send(jpg_data)

        # 6. (可选) 接收服务器回复
        s.settimeout(2.0)
        try:
            res = s.recv(512)
        except:
            pass

        print(" -> [WIFI SUCCESS] Snapshot uploaded to Web!")

    except Exception as e:
        print(" -> [WIFI ERROR] Socket Upload failed:", e)
    finally:
        if s:
            s.close()
        is_uploading = False # 释放锁

# ============================================================================
# 自定义车牌检测类 (YOLO版本) 与识别类
# ============================================================================
class LicenceDetectionApp(AIBase):
    def __init__(self, kmodel_path, model_input_size, confidence_threshold=0.5, nms_threshold=0.2, rgb888p_size=[224,224], display_size=[1920,1080], debug_mode=0):
        super().__init__(kmodel_path, model_input_size, rgb888p_size, debug_mode)
        self.kmodel_path = kmodel_path
        self.model_input_size = model_input_size
        self.confidence_threshold = confidence_threshold
        self.nms_threshold = nms_threshold
        self.max_boxes_num=10
        self.rgb888p_size = [ALIGN_UP(rgb888p_size[0], 16), rgb888p_size[1]]
        self.display_size = [ALIGN_UP(display_size[0], 16), display_size[1]]
        self.debug_mode = debug_mode
        self.ai2d = Ai2d(debug_mode)
        self.ai2d.set_ai2d_dtype(nn.ai2d_format.NCHW_FMT, nn.ai2d_format.NCHW_FMT, np.uint8, np.uint8)

    def config_preprocess(self, input_image_size=None):
        with ScopedTiming("set preprocess config", self.debug_mode > 0):
            ai2d_input_size = input_image_size if input_image_size else self.rgb888p_size
            top, bottom, left, right,_ =letterbox_pad_param(self.rgb888p_size,self.model_input_size)
            self.ai2d.pad([0, 0, 0, 0, top, bottom, left, right], 0, [128, 128, 128])
            self.ai2d.resize(nn.interp_method.tf_bilinear, nn.interp_mode.half_pixel)
            self.ai2d.build([1,3,ai2d_input_size[1],ai2d_input_size[0]],[1,3,self.model_input_size[1],self.model_input_size[0]])

    def postprocess(self, results):
        with ScopedTiming("postprocess", self.debug_mode > 0):
            new_result=results[0][0].transpose()
            det_res = aidemo.yolo_license_plate_det_postprocess(new_result.copy(),[self.rgb888p_size[1],self.rgb888p_size[0]],[self.model_input_size[1],self.model_input_size[0]],[self.display_size[1],self.display_size[0]],self.confidence_threshold,self.nms_threshold,self.max_boxes_num)
            return det_res

class LicenceRecognitionApp(AIBase):
    def __init__(self,kmodel_path,model_input_size,rgb888p_size=[1920,1080],display_size=[1920,1080],debug_mode=0):
        super().__init__(kmodel_path,model_input_size,rgb888p_size,debug_mode)
        self.kmodel_path=kmodel_path
        self.model_input_size=model_input_size
        self.rgb888p_size=[ALIGN_UP(rgb888p_size[0],16),rgb888p_size[1]]
        self.display_size=[ALIGN_UP(display_size[0],16),display_size[1]]
        self.debug_mode=debug_mode
        self.dict_rec = ["挂", "使", "领", "澳", "港", "皖", "沪", "津", "渝", "冀", "晋", "蒙", "辽", "吉", "黑", "苏", "浙", "京", "闽", "赣", "鲁", "豫", "鄂", "湘", "粤", "桂", "琼", "川", "贵", "云", "藏", "陕", "甘", "青", "宁", "新", "警", "学", "0", "1", "2", "3", "4", "5", "6", "7", "8", "9", "A", "B", "C", "D", "E", "F", "G", "H", "J", "K", "L", "M", "N", "P", "Q", "R", "S", "T", "U", "V", "W", "X", "Y", "Z", "_", "-"]
        self.dict_size = len(self.dict_rec)
        self.ai2d=Ai2d(debug_mode)
        self.ai2d.set_ai2d_dtype(nn.ai2d_format.NCHW_FMT,nn.ai2d_format.NCHW_FMT,np.uint8, np.uint8)

    def config_preprocess(self,input_image_size=None):
        with ScopedTiming("set preprocess config",self.debug_mode > 0):
            ai2d_input_size=input_image_size if input_image_size else self.rgb888p_size
            self.ai2d.resize(nn.interp_method.tf_bilinear, nn.interp_mode.half_pixel)
            self.ai2d.build([1,3,ai2d_input_size[1],ai2d_input_size[0]],[1,3,self.model_input_size[1],self.model_input_size[0]])

    def postprocess(self,results):
        with ScopedTiming("postprocess",self.debug_mode > 0):
            output_data=results[0].reshape((-1,self.dict_size))
            max_indices = np.argmax(output_data, axis=1)
            result_str = ""
            for i in range(max_indices.shape[0]):
                index = max_indices[i]
                if index > 0 and (i == 0 or index != max_indices[i - 1]):
                    result_str += self.dict_rec[index - 1]
            return result_str

class LicenceRec:
    def __init__(self,licence_det_kmodel,licence_rec_kmodel,det_input_size,rec_input_size,confidence_threshold=0.25,nms_threshold=0.3,rgb888p_size=[1920,1080],display_size=[1920,1080],debug_mode=0):
        self.licence_det_kmodel=licence_det_kmodel
        self.licence_rec_kmodel=licence_rec_kmodel
        self.det_input_size=det_input_size
        self.rec_input_size=rec_input_size
        self.confidence_threshold=confidence_threshold
        self.nms_threshold=nms_threshold
        self.rgb888p_size=[ALIGN_UP(rgb888p_size[0],16),rgb888p_size[1]]
        self.display_size=[ALIGN_UP(display_size[0],16),display_size[1]]
        self.debug_mode=debug_mode
        self.licence_det=LicenceDetectionApp(self.licence_det_kmodel,model_input_size=self.det_input_size,confidence_threshold=self.confidence_threshold,nms_threshold=self.nms_threshold,rgb888p_size=self.rgb888p_size,display_size=self.display_size,debug_mode=0)
        self.licence_rec=LicenceRecognitionApp(self.licence_rec_kmodel,model_input_size=self.rec_input_size,rgb888p_size=self.rgb888p_size)
        self.licence_det.config_preprocess()

    def run(self,input_np):
        det_boxes=self.licence_det.run(input_np)
        imgs_array_boxes = aidemo.ocr_rec_preprocess(input_np,[self.rgb888p_size[1],self.rgb888p_size[0]],det_boxes[0])
        imgs_array = imgs_array_boxes[0]
        boxes = imgs_array_boxes[1]
        rec_res = []
        for img_array in imgs_array:
            self.licence_rec.config_preprocess(input_image_size=[img_array.shape[3],img_array.shape[2]])
            licence_str=self.licence_rec.run(img_array)
            rec_res.append(licence_str)
            gc.collect()
        return det_boxes,rec_res

    def draw_result(self,pl,det_res,rec_res):
        pl.osd_img.clear()
        det_kps=det_res[0]
        if det_kps:
            for det_index in range(len(det_kps)):
                for j in range(len(det_kps[det_index])):
                    if j%2==0:
                        det_kps[det_index][j]=det_kps[det_index][j]*self.display_size[0]/self.rgb888p_size[0]
                    else:
                        det_kps[det_index][j]=det_kps[det_index][j]*self.display_size[1]/self.rgb888p_size[1]
                for i in range(4):
                    x1=int(det_kps[det_index][(i*2)%8])
                    y1=int(det_kps[det_index][(i*2+1)%8])
                    x2=int(det_kps[det_index][((i+1)*2)%8])
                    y2=int(det_kps[det_index][((i+1)*2+1)%8])
                    pl.osd_img.draw_line(x1,y1,x2,y2,color=(255, 0, 255, 0),thickness=4)
                pl.osd_img.draw_string_advanced(int(det_kps[det_index][6]),int(det_kps[det_index][7]) + 20, 40,rec_res[det_index] , color=(255,255,153,18))

# ============================================================================
#  车牌合法性校验函数
# ============================================================================
def is_valid_plate(plate_str):
    if not plate_str:
        return False
    if len(plate_str) != 7 and len(plate_str) != 8:
        return False
    valid_provinces = ["京", "津", "沪", "渝", "冀", "晋", "蒙", "辽", "吉", "黑",
                       "苏", "浙", "皖", "闽", "赣", "鲁", "豫", "鄂", "湘", "粤",
                       "桂", "琼", "川", "贵", "云", "藏", "陕", "甘", "青", "宁",
                       "新", "港", "澳", "使", "领", "警", "学"]
    if plate_str[0] not in valid_provinces:
        return False
    second_char = plate_str[1]
    if not ('A' <= second_char <= 'Z'):
        return False
    return True

# ============================================================================
#  主程序
# ============================================================================
if __name__=="__main__":

    # --- [配置区] WiFi 与服务器信息 ---
    SSID = "apple"
    PASSWORD = "12345687"
    SERVER_HOST = "api.campusparking.xyz"
    SERVER_PORT = 80
    # 初始化 Pipeline (适配 800x480 屏幕)
    rgb888p_size = [640, 360]
    pl = PipeLine(rgb888p_size=rgb888p_size, display_mode="lcd")
    pl.create()
    display_size = pl.get_display_size() # 得到 [800, 480]

    # --- 屏幕调试信息：WiFi 连接 ---
    pl.osd_img.clear()
    pl.osd_img.draw_string_advanced(200, 200, 40, "Connecting WiFi...", color=(255, 255, 0, 0))
    pl.show_image()

    sta = network.WLAN(network.STA_IF)
    sta.active(True)
    sta.connect(SSID, PASSWORD)

    max_wait = 12
    while not sta.isconnected() and max_wait > 0:
        time.sleep(1)
        max_wait -= 1

    if sta.isconnected():
        msg = "WiFi Connected! IP: " + sta.ifconfig()[0]
        print(msg)
        pl.osd_img.clear()
        pl.osd_img.draw_string_advanced(150, 200, 32, msg, color=(255, 0, 255, 0))
        pl.show_image()
        time.sleep(3) # 持续显示3秒
    else:
        pl.osd_img.clear()
        pl.osd_img.draw_string_advanced(200, 200, 40, "WiFi Connect Failed!", color=(255, 255, 0, 0))
        pl.show_image()
        time.sleep(2)

    # --- 硬件初始化 ---
    fpioa = FPIOA()
    fpioa.set_function(11, FPIOA.UART2_TXD)
    fpioa.set_function(12, FPIOA.UART2_RXD)
    uart = UART(UART.UART2, baudrate=115200, bits=UART.EIGHTBITS, parity=UART.PARITY_NONE, stop=UART.STOPBITS_ONE)

    lr = LicenceRec(
        "/sdcard/examples/kmodel/yolo_license_plate_det.kmodel",
        "/sdcard/examples/kmodel/licence_reco.kmodel",
        det_input_size=[640,640], rec_input_size=[220,32],
        rgb888p_size=rgb888p_size, display_size=display_size
    )

    sent_record = {}
    COOLDOWN_TIME = 30

    try:
        while True:
            img = pl.get_frame()
            det_res, rec_res = lr.run(img)
            lr.draw_result(pl, det_res, rec_res)
            pl.show_image()

            if rec_res:
                current_time = time.time()
                for plate_str in rec_res:
                    plate_str = plate_str.strip()
                    if not is_valid_plate(plate_str): continue

                    if (current_time - sent_record.get(plate_str, 0)) > COOLDOWN_TIME:
                        # 串口发送
                        uart.write(plate_str + "\n")
                        print("[UART SENT] {}".format(plate_str))

                        # 异步上传逻辑
                        if not is_uploading:
                            is_uploading = True
                            try:
                                c, h, w = img.shape[0], img.shape[1], img.shape[2]
                                r, g, b = img[0].flatten(), img[1].flatten(), img[2].flatten()
                                hwc = np.zeros(c * h * w, dtype=np.uint8)
                                hwc[0::3], hwc[1::3], hwc[2::3] = r, g, b
                                img_obj = image.Image(w, h, image.RGB888, data=hwc.tobytes())
                                img_jpeg = img_obj.compress(quality=40)
                                jpg_data = bytes(img_jpeg) if hasattr(img_jpeg, '__bytes__') else bytes(img_jpeg.bytearray())
                                _thread.start_new_thread(upload_task, (jpg_data, SERVER_HOST, SERVER_PORT))
                            except Exception as e:
                                print("Snap Error:", e)
                                is_uploading = False

                        sent_record[plate_str] = current_time
            gc.collect()
    except Exception as e:
        print("Error:", e)
    finally:
        uart.deinit()
        pl.destroy()
