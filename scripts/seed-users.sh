#!/usr/bin/env bash
# Crea/actualiza (idempotente) el grupo perf_users, perf_user_01..18 y makise en la RÉPLICA.
set -euo pipefail

NS=kiwi
POD=kiwi-perf-0
[[ "$POD" == "kiwi-perf-0" ]] || { echo "Pod no permitido: $POD" >&2; exit 1; }

secret_val() { kubectl get secret k6-perf-credentials -n "$NS" -o "jsonpath={.data.$1}" | base64 -d; }

PY_CODE=$(cat <<'PY'
import os
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.db.models import Q

makise_user, makise_pw, perf_pw = os.environ["MK_USER"].strip().lower(), os.environ["MK_PW"], os.environ["PERF_PW"]
User = get_user_model()

apps = ["testcases", "testruns", "testplans", "bugs", "management"]
perms = Permission.objects.filter(content_type__app_label__in=apps).filter(
    ~Q(content_type__model__startswith="historical") | Q(codename__startswith="view_"))
group, _ = Group.objects.get_or_create(name="perf_users")
group.permissions.set(perms)

for i in range(1, 19):
    name = "perf_user_%02d" % i
    u, _ = User.objects.get_or_create(username=name, defaults={"email": name + "@example.invalid"})
    u.is_active, u.is_staff, u.is_superuser = True, False, False
    u.set_password(perf_pw)
    u.save()
    u.groups.add(group)

m, _ = User.objects.get_or_create(username=makise_user, defaults={"email": makise_user + "@example.invalid"})
m.is_active, m.is_staff, m.is_superuser = True, True, True
m.set_password(makise_pw)
m.save()
print("perf_users: %d permisos; perf_user_01..18 y makise listos" % perms.count())
PY
)

# Las contraseñas viajan por stdin (líneas 1-3), el código Python desde la línea 4; nada en argv.
{
  secret_val makise-username; echo
  secret_val makise-password; echo
  secret_val perf-user-password; echo
  printf '%s\n' "$PY_CODE"
} | kubectl exec -i -n "$NS" "$POD" -c kiwi -- /venv/bin/python /Kiwi/manage.py shell -c '
import os, sys
d = sys.stdin.read().split("\n")
os.environ["MK_USER"], os.environ["MK_PW"], os.environ["PERF_PW"] = d[0], d[1], d[2]
exec("\n".join(d[3:]))
'
