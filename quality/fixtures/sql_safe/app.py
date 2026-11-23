"""合成样例：唯一数据库查询使用参数绑定。"""

def find_customer(conn, name):
    return conn.execute("SELECT * FROM customers WHERE name = ?", (name,)).fetchall()
