#!/bin/sh
# The delivery gate's buckets and users (M.12). Run in the minio/mc image by scripts/delivery-gate.sh:
#   setup     two buckets; gate-rw (read, write, delete on delivery-gate), gate-putonly (write only),
#             gate-other (read, write, delete on delivery-gate-2), secret keys "$GATE_SECRET-<user>"
#   teardown  removes all of it
set -eu

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null

case "$1" in
setup)
    for bucket in delivery-gate delivery-gate-2; do mc mb --ignore-existing "local/$bucket"; done
    for user in rw putonly other; do
        mc admin policy create local "gate-$user" "/gate/policy-$user.json"
        mc admin user add local "gate-$user" "$GATE_SECRET-$user" >/dev/null
        case "$(mc admin user info local "gate-$user")" in
            *"PolicyName: gate-$user"*) ;;
            *) mc admin policy attach local "gate-$user" --user "gate-$user" >/dev/null ;;
        esac
    done
    ;;
teardown)
    for user in rw putonly other; do
        mc admin user rm local "gate-$user" || true
        mc admin policy rm local "gate-$user" || true
    done
    for bucket in delivery-gate delivery-gate-2; do mc rb --force "local/$bucket" || true; done
    ;;
*)
    echo "usage: minio.sh setup|teardown" >&2
    exit 2
    ;;
esac
