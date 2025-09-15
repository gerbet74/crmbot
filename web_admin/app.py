# web_admin/app.py
import os
import sqlite3
from flask import Flask, render_template, request, redirect, url_for, session, send_file
from datetime import datetime
import pandas as pd

app = Flask(__name__)
app.secret_key = "alex7474"  # 🔐 Замени на свой

DB_PATH = os.path.join(os.path.dirname(__file__), "booking.db")  # 👈 Путь к твоей базе от бота

# --- АДМИН ПАРОЛЬ ---
ADMIN_PASSWORD = "grenader74"  # 🔐 ЗАМЕНИ ЭТО НА СВОЙ ПАРОЛЬ!

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        password = request.form['password']
        if password == ADMIN_PASSWORD:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            return render_template('login.html', error="Неверный пароль")

    # Если метод GET — просто показываем форму входа
    if 'logged_in' in session:
        return redirect(url_for('dashboard'))
    return render_template('login.html')

@app.route('/login', methods=['POST'])
def do_login():
    password = request.form['password']
    if password == ADMIN_PASSWORD:
        session['logged_in'] = True
        return redirect(url_for('dashboard'))
    else:
        return render_template('login.html', error="Неверный пароль")

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'logged_in' not in session:
        return redirect(url_for('login'))

    conn = get_db()
    c = conn.cursor()
    c.execute('''
        SELECT 
            b.id, 
            u.username, 
            u.first_name,
            b.specialization, 
            b.direction, 
            b.instrument, 
            b.date, 
            b.time_slot, 
            b.status, 
            b.price
        FROM bookings b
        LEFT JOIN users u ON b.user_id = u.user_id
        ORDER BY b.date DESC, b.time_slot DESC
    ''')
    bookings = c.fetchall()
    conn.close()

    return render_template('index.html', bookings=bookings)

@app.route('/export')
def export_excel():
    if 'logged_in' not in session:
        return redirect(url_for('login'))

    conn = get_db()
    df = pd.read_sql_query('''
        SELECT 
            u.username AS "Имя пользователя",
            u.first_name AS "Имя",
            b.specialization AS "Специализация",
            b.direction AS "Направление",
            b.instrument AS "Инструмент",
            b.date AS "Дата",
            b.time_slot AS "Время",
            b.status AS "Статус",
            b.price AS "Цена"
        FROM bookings b
        LEFT JOIN users u ON b.user_id = u.user_id
        ORDER BY b.date DESC, b.time_slot DESC
    ''', conn)
    conn.close()

    filename = f"booking_export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
    df.to_excel(filename, index=False, sheet_name='Бронирования')

    return send_file(filename, as_attachment=True)

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False)