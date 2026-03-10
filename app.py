from flask import Flask, render_template, request, jsonify
import sqlite3
import datetime
import os  # --- 导入 os 库 ---

app = Flask(__name__)

# --- 确保有一个 static 文件夹来存图片 ---
if not os.path.exists('static'):
    os.makedirs('static')
# ---  全局变量存储温湿度 ---
current_temp = "--"
current_hum = "--"

# --- 数据库初始化 ---
def init_db():
    # 确保保存相册的文件夹存在
    if not os.path.exists('static/snapshots'):
        os.makedirs('static/snapshots')
    #每次运行清除旧的抓拍记录，保持相册清爽
    for f in os.listdir('static/snapshots'):
        os.remove(os.path.join('static/snapshots', f))   
    conn = sqlite3.connect('parking.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS slots 
                 (id INTEGER PRIMARY KEY, occupied INTEGER, charging INTEGER, 
                  license_plate TEXT, start_time TEXT, end_time TEXT, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS charging_logs 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
                 
    # --- 创建抓拍图片相册表 ---
    c.execute('''CREATE TABLE IF NOT EXISTS snapshot_logs 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, image_url TEXT, timestamp TEXT)''')

    # ========================================================
    # ---清空数据库里的旧抓拍记录，保持和文件夹同步 ---
    c.execute('DELETE FROM snapshot_logs')
    # ========================================================
    # 初始化车位数据，如果表里没有记录的话     
    c.execute('SELECT count(*) FROM slots')
    if c.fetchone()[0] == 0:
        for i in range(1, 5):
            c.execute('INSERT INTO slots (id, occupied, charging, license_plate, start_time, end_time, password) VALUES (?, 0, 0, NULL, NULL, NULL, NULL)', (i,))
    conn.commit()
    conn.close()

@app.route('/')
def index():
    return render_template('index.html')

# =======================================================
# ---  接收 K230 抓拍图片并生成历史记录 ---
# =======================================================
@app.route('/upload_frame', methods=['POST'])
def upload_frame():
    img_data = request.get_data()
    
    if img_data and len(img_data) > 0:
        # 获取当前时间
        now = datetime.datetime.now()
        time_str = now.strftime('%Y-%m-%d %H:%M:%S')
        # 以时间戳命名图片，防止覆盖 (例如: 20260305_163000.jpg)
        filename = now.strftime('%Y%m%d_%H%M%S') + '.jpg'
        filepath = os.path.join('static', 'snapshots', filename)
        
        # 写入图片到本地
        with open(filepath, 'wb') as f:
            f.write(img_data)
            
        # 将图片路径和时间存入数据库
        conn = sqlite3.connect('parking.db')
        c = conn.cursor()
        c.execute('INSERT INTO snapshot_logs (image_url, timestamp) VALUES (?, ?)', ('/static/snapshots/' + filename, time_str))
        conn.commit()
        conn.close()
        
        print(f">>> [Snapshot Saved] {filename}")
        return "Image Saved OK", 200
    return "Failed", 400

# =======================================================
# --- [新增] 前端获取抓拍相册列表接口 ---
# =======================================================
@app.route('/api/snapshots')
def get_snapshots():
    conn = sqlite3.connect('parking.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    # 按时间倒序，获取最新的 20 张抓拍记录
    c.execute('SELECT image_url, timestamp FROM snapshot_logs ORDER BY id DESC LIMIT 20')
    rows = c.fetchall()
    conn.close()
    
    logs = [{'url': row['image_url'], 'time': row['timestamp']} for row in rows]
    return jsonify(logs)

# --- 获取实时状态 ---
@app.route('/api/status')
def get_status():
    conn = sqlite3.connect('parking.db')
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    c.execute('SELECT * FROM slots')
    rows = c.fetchall()
    
    data = []
    now = datetime.datetime.now()
    
    for row in rows:
        row_plate = row['license_plate']
        start_str = row['start_time']
        end_str = row['end_time']
        should_cancel = False
        
        if row_plate and start_str:
            try:
                start_dt = datetime.datetime.strptime(start_str, '%Y-%m-%d %H:%M:%S')
                end_dt = datetime.datetime.strptime(end_str, '%Y-%m-%d %H:%M:%S')
                if now > end_dt: should_cancel = True
                timeout_limit = start_dt + datetime.timedelta(minutes=5)
                if now > timeout_limit and row['occupied'] == 0: should_cancel = True
            except: pass

        if should_cancel:
            # 清理时同时重置密码字段
            c.execute('UPDATE slots SET license_plate = NULL, start_time = NULL, end_time = NULL, password = NULL WHERE id = ?', (row['id'],))
            conn.commit()
            row_plate = None

        data.append({
            'id': row['id'],
            'occupied': bool(row['occupied']),
            'charging': bool(row['charging']),
            'license_plate': row_plate,
            'start_time': start_str,
            'end_time': end_str
        })
    conn.close()
    return jsonify(data)

# --- 新增: 接收温湿度数据接口 (给ESP32用) ---
@app.route('/update_env')
def update_env():
    global current_temp, current_hum
    current_temp = request.args.get('temp', '--')
    current_hum = request.args.get('hum', '--')
    return "OK"

# --- 新增: 前端获取温湿度接口 (给网页用) ---
@app.route('/api/env')
def api_env():
    return jsonify({'temp': current_temp, 'hum': current_hum})

# --- 新增: 给 ESP32 验证车牌用的接口 ---
@app.route('/api/verify', methods=['POST'])
def verify_plate():
    # 获取 ESP32 发来的纯文本车牌号 (使用 utf-8 解码处理中文)
    plate_from_esp32 = request.data.decode('utf-8').strip()
    
    if not plate_from_esp32:
        return "ERROR: Empty Plate"

    conn = sqlite3.connect('parking.db')
    c = conn.cursor()
    
    # 在数据库的 slots 表里寻找这个车牌
    c.execute('SELECT id FROM slots WHERE license_plate = ?', (plate_from_esp32,))
    row = c.fetchone()
    conn.close()
    
    # 如果找到了，返回 "OK,车位号"
    if row:
        slot_id = row[0]
        return f"OK,{slot_id}"
    else:
        # 如果没找到，说明没预约，或者是乱停的
        return "FAIL"
    
# --- 预约/取消接口 ---
@app.route('/api/reserve', methods=['POST'])
def reserve_slot():
    data = request.json
    slot_id = data.get('id')
    plate = data.get('plate')
    action = data.get('action')
    pin = data.get('pin') # 获取前端传来的密码
    
    conn = sqlite3.connect('parking.db')
    c = conn.cursor()
    
    c.execute('SELECT occupied, license_plate, password FROM slots WHERE id = ?', (slot_id,))
    row = c.fetchone()
    
    if not row:
        conn.close()
        return jsonify({'success': False, 'message': 'Slot not found'})

    if action == 'reserve':
        if row[0] == 1: return jsonify({'success': False, 'message': 'Slot is physically occupied!'})
        if row[1] is not None: return jsonify({'success': False, 'message': 'Slot already reserved!'})
        
        # 验证密码格式
        if not pin or len(pin) != 4 or not pin.isdigit():
            return jsonify({'success': False, 'message': 'Please set a 4-digit PIN!'})

        try:
            start_dt = datetime.datetime.strptime(data.get('startTime'), '%Y-%m-%dT%H:%M')
            if data.get('mode') == 'duration':
                h, m = int(data.get('hours', 0)), int(data.get('minutes', 0))
                if h == 0 and m == 0: return jsonify({'success': False, 'message': 'Invalid duration!'})
                end_dt = start_dt + datetime.timedelta(hours=h, minutes=m)
            else:
                end_dt = datetime.datetime.strptime(data.get('endTime'), '%Y-%m-%dT%H:%M')
            
            if end_dt <= start_dt: return jsonify({'success': False, 'message': 'Invalid time range!'})

            # 存储密码
            c.execute('UPDATE slots SET license_plate = ?, start_time = ?, end_time = ?, password = ? WHERE id = ?', 
                      (plate, start_dt.strftime('%Y-%m-%d %H:%M:%S'), end_dt.strftime('%Y-%m-%d %H:%M:%S'), pin, slot_id))
            msg = "Reservation Confirmed"
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})
        
    elif action == 'cancel':
        # 核心逻辑：比对数据库中的密码
        saved_pin = row[2]
        if saved_pin and pin != saved_pin:
            conn.close()
            return jsonify({'success': False, 'message': 'Incorrect PIN! Cancel failed.'})

        c.execute('UPDATE slots SET license_plate = NULL, start_time = NULL, end_time = NULL, password = NULL WHERE id = ?', (slot_id,))
        msg = "Reservation Canceled"
    
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'message': msg})

@app.route('/api/stats')
def get_stats():
    conn = sqlite3.connect('parking.db')
    c = conn.cursor()
    # 加入 datetime(timestamp, 'localtime') 进行时区转换
    c.execute('''SELECT strftime('%H', datetime(timestamp, 'localtime')) as hour, COUNT(*) FROM charging_logs GROUP BY hour ORDER BY hour''')
    rows = c.fetchall()
    conn.close()
    hours_data = {str(i).zfill(2): 0 for i in range(24)}
    for row in rows: hours_data[row[0]] = row[1]
    return jsonify(list(hours_data.values()))

@app.route('/update', methods=['GET'])
def update_slot():
    slot_id, occupied, charging = request.args.get('id'), request.args.get('occupied'), request.args.get('charging')
    response_msg = "OK" # 默认回复 OK
    
    if slot_id:
        conn = sqlite3.connect('parking.db')
        c = conn.cursor()
        
        if occupied is not None: 
            c.execute('UPDATE slots SET occupied = ? WHERE id = ?', (occupied, slot_id))
            
            # --- 核心逻辑: 检查是否违停 (被占用且没有预约车牌) ---
            if int(occupied) == 1:
                c.execute('SELECT license_plate FROM slots WHERE id = ?', (slot_id,))
                row = c.fetchone()
                if row and row[0] is None:
                    response_msg = "ALARM" # 未预约却被占用，下发报警指令给ESP32！
                    
        if charging is not None:
            c.execute('SELECT charging FROM slots WHERE id = ?', (slot_id,))
            if int(charging) == 1 and c.fetchone()[0] == 0:
                c.execute('INSERT INTO charging_logs (slot_id) VALUES (?)', (slot_id,))
            c.execute('UPDATE slots SET charging = ? WHERE id = ?', (charging, slot_id))
            
        conn.commit()
        conn.close()
        return response_msg
    return "Error"

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5001)