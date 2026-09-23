import pymysql

connection = pymysql.connect(
    host="localhost",
    user="root",
    password="Cmsc250!",
    database="baseball_charting"
)

cursor = connection.cursor()
cursor.execute("SHOW TABLES;")

tables = cursor.fetchall()
print("Tables in database:")
for table in tables:
    print(" -", table[0])

connection.close()