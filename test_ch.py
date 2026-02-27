import clickhouse_connect
print(clickhouse_connect.get_client(host='localhost', port=8123, username='default', password='').command('SELECT 1'))
