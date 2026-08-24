import psycopg

base = "host=localhost port=5433 user=finpay password=finpay"
c = psycopg.connect(base + " dbname=finpay", connect_timeout=5)
print("connected:", c.execute("SELECT current_database(), current_user").fetchone())
c.close()

created = False
for admin_db in ("postgres", "finpay"):
    try:
        a = psycopg.connect(base + " dbname=" + admin_db, connect_timeout=5, autocommit=True)
        cur = a.cursor()
        exists = cur.execute("SELECT 1 FROM pg_database WHERE datname='finpay_topup_test'").fetchone()
        if not exists:
            cur.execute("CREATE DATABASE finpay_topup_test")
            print("created finpay_topup_test")
        else:
            print("finpay_topup_test already exists")
        a.close()
        created = True
        break
    except Exception as e:
        print("admin_db", admin_db, "error:", repr(e))

print("OK" if created else "COULD_NOT_CREATE")
