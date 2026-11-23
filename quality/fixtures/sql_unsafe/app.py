"""合成样例：查询参数可能来自不可信调用方。"""

def find_customer(conn, name):
    query = "SELECT * FROM customers WHERE name = '" + name + "'"
    return conn.execute(query).fetchall()
