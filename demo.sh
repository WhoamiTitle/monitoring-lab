#!/bin/sh

# Запуск ограниченной нагрузки на один из учебных контейнеров.
set -eu

usage() {
    cat <<'EOF'
Использование: ./demo.sh <сценарий> <web-1|web-2> [секунды]

Сценарии:
  cpu      процессорная нагрузка, по умолчанию 20 секунд
  memory   занять 32 MiB в /dev/shm, по умолчанию на 30 секунд
  disk     записать 16 MiB в /tmp, по умолчанию на 30 секунд
  traffic  дополнительные HTTP-запросы, по умолчанию 20 секунд
  slow     запросы с задержкой примерно 0,8 с, по умолчанию 20 секунд
  errors   ответы HTTP 500, по умолчанию 20 секунд

Также можно использовать имена ram и network вместо memory и traffic.
Длительность задаётся целым числом от 1 до 120 секунд.

Примеры:
  ./demo.sh cpu web-1
  ./demo.sh memory web-2 45
  ./demo.sh traffic web-1 30
  ./demo.sh slow web-1 30
  ./demo.sh errors web-2 30

Выбранный web-контейнер должен быть запущен. Временные файлы удаляются
после завершения сценария. Для traffic, slow и errors используется временный контейнер load.
Для досрочной остановки сценария нажми Ctrl+C.
EOF
}

fail() {
    printf 'Ошибка: %s\n' "$1" >&2
    exit 2
}

if [ "$#" -eq 1 ] && { [ "$1" = '-h' ] || [ "$1" = '--help' ]; }; then
    usage
    exit 0
fi

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ]; then
    usage >&2
    exit 2
fi

scenario=$1
service=$2

case "$scenario" in
    cpu) default_seconds=20 ;;
    memory|ram) scenario=memory; default_seconds=30 ;;
    disk) default_seconds=30 ;;
    traffic|network) scenario=traffic; default_seconds=20 ;;
    slow|errors) default_seconds=20 ;;
    *) fail 'Выбери сценарий: cpu, memory, disk, traffic, slow или errors.' ;;
esac

case "$service" in
    web-1|web-2) ;;
    *) fail 'Выбери приложение web-1 или web-2.' ;;
esac

duration_seconds=${3-$default_seconds}
case "$duration_seconds" in
    ''|*[!0-9]*|0|0[0-9]*)
        fail 'Длительность должна быть целым числом от 1 до 120 секунд без ведущих нулей.'
        ;;
esac
if [ "${#duration_seconds}" -gt 3 ] || [ "$duration_seconds" -gt 120 ]; then
    fail 'Длительность должна быть целым числом от 1 до 120 секунд.'
fi

project_dir=$(CDPATH= cd -P "$(dirname "$0")" && pwd)
cd "$project_dir"

compose() {
    docker compose --project-directory "$project_dir" "$@"
}

if ! running_container=$(compose ps --status running --quiet "$service"); then
    printf 'Не удалось проверить состояние Docker Compose.\n' >&2
    exit 1
fi
if [ -z "$running_container" ]; then
    printf 'Сервис %s не запущен. Сначала запусти нужный вариант стенда.\n' "$service" >&2
    exit 1
fi

printf 'Сценарий: %s; приложение: %s; длительность: %s с.\n' \
    "$scenario" "$service" "$duration_seconds"

case "$scenario" in
    cpu|memory|disk)
        # У каждого запуска свой каталог для сигнала отмены внутри контейнера.
        session_dir=$(compose exec -T --interactive=false "$service" \
            mktemp -d /tmp/monitoring-lab-task.XXXXXX)
        case "$session_dir" in
            /tmp/monitoring-lab-task.??????) ;;
            *)
                printf 'Не удалось создать каталог сценария в контейнере.\n' >&2
                exit 1
                ;;
        esac

        cancel_container_work() {
            status=$1
            trap '' INT TERM
            compose exec -T --interactive=false "$service" sh -c '
                session=$1
                [ -d "$session" ] || exit 0
                : > "$session/cancel"
                attempts=0
                while [ -d "$session" ] && [ "$attempts" -lt 4 ]; do
                    sleep 1
                    attempts=$((attempts + 1))
                done
                # Если нагрузка не успела запуститься, удаляем пустой каталог.
                if [ -d "$session" ] && [ ! -e "$session/started" ]; then
                    rm -f "$session/cancel"
                    rmdir "$session" 2>/dev/null || :
                fi
            ' demo "$session_dir" >/dev/null 2>&1 || :
            exit "$status"
        }

        trap 'cancel_container_work 130' INT
        trap 'cancel_container_work 143' TERM
        if compose exec -T --interactive=false "$service" sh -eu -c '
            task=$1
            duration=$2
            session=$3
            worker=
            file=
            cleanup() {
                if [ -n "$worker" ]; then
                    kill "$worker" 2>/dev/null || :
                    wait "$worker" 2>/dev/null || :
                fi
                if [ -n "$file" ]; then
                    rm -f "$file"
                fi
                rm -f "$session/cancel" "$session/started"
                rmdir "$session" 2>/dev/null || :
            }
            trap cleanup 0
            trap "exit 130" 2
            trap "exit 143" 15

            [ -d "$session" ] || exit 1
            : > "$session/started"
            [ ! -e "$session/cancel" ] || exit 130

            case "$task" in
                cpu)
                    # Рабочий процесс учитывается в CPU контейнера.
                    while :; do :; done &
                    worker=$!
                    ;;
                memory)
                    # /dev/shm использует память cgroup контейнера.
                    file=$(mktemp /dev/shm/monitoring-lab-ram.XXXXXX)
                    dd if=/dev/zero of="$file" bs=1M count=32
                    ;;
                disk)
                    # Файл остаётся на диске, пока его можно увидеть на графике.
                    file=$(mktemp /tmp/monitoring-lab-disk.XXXXXX)
                    dd if=/dev/zero of="$file" bs=1M count=16 conv=fsync
                    ;;
            esac

            elapsed=0
            while [ "$elapsed" -lt "$duration" ] && [ ! -e "$session/cancel" ]; do
                sleep 1
                elapsed=$((elapsed + 1))
            done
            [ ! -e "$session/cancel" ] || exit 130
        ' demo "$scenario" "$duration_seconds" "$session_dir"; then
            trap - INT TERM
        else
            status=$?
            cancel_container_work "$status"
        fi
        ;;
    traffic|slow|errors)
        case "$scenario" in
            traffic) path=/; expected=ok ;;
            slow) path=/__demo/slow; expected=ok ;;
            errors) path=/__demo/error; expected=500 ;;
        esac
        # Уникальное имя и метка позволяют остановить только созданный нами клиент.
        traffic_dir=$(mktemp -d "${TMPDIR:-/tmp}/monitoring-lab-traffic.XXXXXX")
        traffic_token="${traffic_dir##*.}-$$"
        traffic_name="monitoring-lab-traffic-$traffic_token"

        cancel_traffic() {
            status=$1
            trap '' INT TERM
            actual_token=$(docker inspect \
                --format '{{ index .Config.Labels "monitoring-lab.demo-token" }}' \
                "$traffic_name" 2>/dev/null || :)
            if [ "$actual_token" = "$traffic_token" ]; then
                docker stop --time 1 "$traffic_name" >/dev/null 2>&1 || :
            fi
            rmdir "$traffic_dir" 2>/dev/null || :
            exit "$status"
        }

        trap 'cancel_traffic 130' INT
        trap 'cancel_traffic 143' TERM
        # Дополнительный клиент не запускает зависимости и не удаляет другие сервисы.
        if COMPOSE_IGNORE_ORPHANS=true COMPOSE_REMOVE_ORPHANS=false \
            docker compose --project-directory "$project_dir" \
            --profile load run --rm --no-deps -T --interactive=false \
            --name "$traffic_name" --label "monitoring-lab.demo-token=$traffic_token" \
            --entrypoint /bin/sh load -eu -c '
                target=$1
                end_time=$(( $(date +%s) + $2 + 1 ))
                expected=$3
                requests=0
                while [ "$(date +%s)" -lt "$end_time" ]; do
                    if [ "$expected" = 500 ]; then
                        if result=$(wget -S -T 2 -O /dev/null "$target" 2>&1); then
                            printf "Ожидался HTTP 500, получен успешный ответ.\n" >&2
                            exit 1
                        else
                            case "$result" in
                                *"HTTP/1.1 500"*|*"HTTP/1.0 500"*) ;;
                                *) printf "Ожидался HTTP 500; ошибка запроса: %s\n" "$result" >&2; exit 1 ;;
                            esac
                        fi
                    elif ! wget -q -T 2 -O /dev/null "$target"; then
                        printf "Не удалось получить HTTP-ответ от %s.\n" "$target" >&2
                        exit 1
                    fi
                    requests=$((requests + 1))
                    sleep 0.01
                done
                printf "Завершённых запросов (ожидаемый ответ %s): %s\n" "$expected" "$requests"
            ' demo "http://$service:80$path" "$duration_seconds" "$expected"; then
            trap - INT TERM
            rmdir "$traffic_dir" 2>/dev/null || :
        else
            status=$?
            cancel_traffic "$status"
        fi
        ;;
esac

printf 'Сценарий %s для %s завершён.\n' "$scenario" "$service"
