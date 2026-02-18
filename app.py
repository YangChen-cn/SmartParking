from flask import Flask, render_template, request, jsonify
import sqlite3
import datetime

app = Flask(__name__)

# --- 数据库初始化 ---
def init_db():
    conn = sqlite3.connect('parking.db')
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS slots 
                 (id INTEGER PRIMARY KEY, occupied INTEGER, charging INTEGER, 
                  license_plate TEXT, start_time TEXT, end_time TEXT, password TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS charging_logs 
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, slot_id INTEGER, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    c.execute('SELECT count(*) FROM slots')
    if c.fetchone()[0] == 0:
        for i in range(1, 5):
            c.execute('INSERT INTO slots (id, occupied, charging, license_plate, start_time, end_time, password) VALUES (?, 0, 0, NULL, NULL, NULL, NULL)', (i,))
        conn.commit()
    conn.close()

@app.route('/')
def index():
    return render_template('index.html')

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

# --- 其余接口保持不变 ---
@app.route('/api/stats')
def get_stats():
    conn = sqlite3.connect('parking.db')
    c = conn.cursor()
    c.execute('''SELECT strftime('%H', timestamp) as hour, COUNT(*) FROM charging_logs GROUP BY hour ORDER BY hour''')
    rows = c.fetchall()
    conn.close()
    hours_data = {str(i).zfill(2): 0 for i in range(24)}
    for row in rows: hours_data[row[0]] = row[1]
    return jsonify(list(hours_data.values()))

@app.route('/update', methods=['GET'])
def update_slot():
    slot_id, occupied, charging = request.args.get('id'), request.args.get('occupied'), request.args.get('charging')
    if slot_id:
        conn = sqlite3.connect('parking.db')
        c = conn.cursor()
        if occupied is not None: c.execute('UPDATE slots SET occupied = ? WHERE id = ?', (occupied, slot_id))
        if charging is not None:
            c.execute('SELECT charging FROM slots WHERE id = ?', (slot_id,))
            if int(charging) == 1 and c.fetchone()[0] == 0:
                c.execute('INSERT INTO charging_logs (slot_id) VALUES (?)', (slot_id,))
            c.execute('UPDATE slots SET charging = ? WHERE id = ?', (charging, slot_id))
        conn.commit()
        conn.close()
        return "Success"
    return "Error"

if __name__ == '__main__':
    init_db()
    app.run(debug=True, host='0.0.0.0', port=5001)