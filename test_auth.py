from core.auth import AuthManager

auth = AuthManager(jwt_secret="clave-local-de-prueba-2026")

try:
    auth.create_user("admin", "ClaveSegura123!", role="admin")
except Exception as e:
    print("Aviso al crear usuario:", e)

print("Intento fallido:", auth.login("admin", "clave_mala"))
print("Login correcto:", auth.login("admin", "ClaveSegura123!"))
