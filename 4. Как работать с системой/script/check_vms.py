import yaml
import subprocess
import json
import time
import socket
import base64
import sys
import concurrent.futures

CONFIG_FILE = "config.yaml"
DOCKER_WAIT_TIME = 120 

# ==========================================
# Настройки цветов (ANSI Colors)
# ==========================================
C_RESET   = '\033[0m'
C_BOLD    = '\033[1m'
C_GREEN   = '\033[92m'
C_RED     = '\033[91m'
C_YELLOW  = '\033[93m'
C_CYAN    = '\033[96m'
C_BLUE    = '\033[94m'
C_MAGENTA = '\033[95m'
C_GRAY    = '\033[90m'

def print_header(text):
    print(f"\n{C_BOLD}{C_CYAN}=== {text} ==={C_RESET}")

def pad_ansi(raw_text, colored_text, width):
    """Вычисляет длину текста без учета ANSI-кодов и добавляет ровные пробелы"""
    padding = max(0, width - len(str(raw_text)))
    return colored_text + " " * padding

def animated_progress_bar(seconds):
    bar_length = 40
    print_header(f"Ожидание {seconds} секунд (Инициализация системы, Docker и сетей)")
    for i in range(seconds, -1, -1):
        progress = (seconds - i) / seconds
        filled = int(bar_length * progress)
        bar = '█' * filled + '░' * (bar_length - filled)
        sys.stdout.write(f"\r{C_YELLOW}⏳ Запуск инфраструктуры: [{bar}] Осталось: {i:03d} сек...{C_RESET}")
        sys.stdout.flush()
        if i > 0: time.sleep(1)
    print(f"\n{C_GREEN}✅ Все системы должны быть готовы! Начало сканирования...{C_RESET}")

def get_vm_state(vm_name):
    try:
        out = subprocess.check_output(['virsh', 'domstate', vm_name], stderr=subprocess.DEVNULL)
        return out.decode('utf-8').strip()
    except subprocess.CalledProcessError:
        return "unknown"

def start_vm(vm_name):
    try:
        subprocess.check_call(['virsh', 'start', vm_name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except subprocess.CalledProcessError:
        return False

def wait_for_guest_agent(vm_name, timeout=180):
    print(f"[{C_BLUE}{vm_name}{C_RESET}] Ожидание готовности гостевого агента...")
    start_time = time.time()
    ping_cmd = json.dumps({"execute": "guest-ping"})
    
    while time.time() - start_time < timeout:
        try:
            subprocess.check_call(['virsh', 'qemu-agent-command', vm_name, ping_cmd], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
            print(f"[{C_BLUE}{vm_name}{C_RESET}] {C_GREEN}✅ Агент отвечает!{C_RESET}")
            return True
        except subprocess.CalledProcessError:
            time.sleep(2)
            
    print(f"[{C_BLUE}{vm_name}{C_RESET}] {C_RED}❌ Превышено время ожидания.{C_RESET}")
    return False

def run_qemu_agent_command(vm_name, command):
    try:
        req = {
            "execute": "guest-exec",
            "arguments": {"path": "/bin/sh", "arg": ["-c", command], "capture-output": True}
        }
        out = subprocess.check_output(['virsh', 'qemu-agent-command', vm_name, json.dumps(req)], stderr=subprocess.DEVNULL)
        pid = json.loads(out.decode('utf-8'))['return']['pid']

        while True:
            req_stat = {"execute": "guest-exec-status", "arguments": {"pid": pid}}
            out_stat = subprocess.check_output(['virsh', 'qemu-agent-command', vm_name, json.dumps(req_stat)], stderr=subprocess.DEVNULL)
            res = json.loads(out_stat.decode('utf-8'))['return']
            
            if res.get('exited'):
                out_data = res.get('out-data', '')
                stdout = base64.b64decode(out_data).decode('utf-8') if out_data else ''
                return res['exitcode'], stdout, ''
            time.sleep(0.3)
    except Exception as e:
        return -1, "", str(e)

def configure_network(vm):
    vm_name = vm['name']
    iface = vm.get('interface')
    ip_cidr = vm.get('ip')
    gateway = vm.get('gateway')
    if not iface or not gateway: return
    clean_ip = ip_cidr.split('/')[0]
    check_cmd = f"ip -4 addr show dev {iface} | grep '{clean_ip}'"
    
    for _ in range(5):
        exitcode, stdout, _ = run_qemu_agent_command(vm_name, check_cmd)
        if exitcode == 0:
            print(f"[{C_BLUE}{vm_name}{C_RESET}] {C_GREEN}✅ IP {clean_ip} присутствует на {iface}.{C_RESET}")
            return
        time.sleep(1.5)

    print(f"[{C_BLUE}{vm_name}{C_RESET}] {C_YELLOW}Добавление IP ({iface} -> {ip_cidr})...{C_RESET}")
    network_cmd = f"ip link set dev {iface} up && ip addr add {ip_cidr} dev {iface} || true && ip route add default via {gateway} || true"
    exitcode, _, _ = run_qemu_agent_command(vm_name, network_cmd)

    if exitcode == 0:
        print(f"[{C_BLUE}{vm_name}{C_RESET}] {C_GREEN}✅ IP успешно добавлен.{C_RESET}")
    else:
        print(f"[{C_BLUE}{vm_name}{C_RESET}] {C_RED}❌ Ошибка настройки сети.{C_RESET}")

def get_vm_metrics(vm_name):
    """Сбор метрик и поиск упавших (dead/exited) Docker контейнеров"""
    metric_cmd = """
    cpu=$(vmstat 1 2 | tail -1 | awk '{print 100-$15}')
    total_mem=$(free -m | grep Mem | awk '{print $2}')
    used_mem=$(free -m | grep Mem | awk '{print $3}')
    [ "$total_mem" -gt 0 ] && mem=$((used_mem * 100 / total_mem)) || mem=0
    disk=$(df -h / | awk 'NR==2 {print $5}' | tr -d '%')
    
    dock_run=$(docker ps -q 2>/dev/null | wc -l || echo 0)
    dock_err=$(docker ps -q -f status=exited -f status=dead 2>/dev/null | wc -l || echo 0)
    
    up=$(cat /proc/uptime | awk '{printf "%dd %02dh", $1/86400, ($1%86400)/3600}')
    logs=$(du -sh /home/artm1904/tpotce/data 2>/dev/null | awk '{print $1}')
    [ -z "$logs" ] && logs="0M"
    
    echo "${cpu};${mem};${disk};${dock_run};${dock_err};${up};${logs}"
    """
    exitcode, stdout, _ = run_qemu_agent_command(vm_name, metric_cmd)
    if exitcode == 0 and stdout:
        parts = stdout.strip().split(';')
        if len(parts) == 7: return parts
    return ["-", "-", "-", "-", "-", "-", "-"]

def check_ping(ip):
    try:
        subprocess.check_output(['ping', '-c', '1', '-W', '1', ip], stderr=subprocess.DEVNULL)
        return True
    except:
        return False

def check_port(ip, port):
    try:
        with socket.create_connection((ip, port), timeout=1.5): return True
    except:
        return False

def scan_single_vm(vm, ready_vms):
    vm_name = vm['name']
    is_docker = vm.get('check_only', False)
    clean_ip = vm['ip'].split('/')[0] 
    is_pinging = check_ping(clean_ip)
    cpu, mem, disk, dock_run, dock_err, uptime, logs = ("-", "-", "-", "-", "-", "-", "-")

    if not is_docker and vm_name in ready_vms:
        metrics = get_vm_metrics(vm_name)
        if len(metrics) == 7:
            cpu, mem, disk, dock_run, dock_err, uptime, logs = metrics
    
    services_status = []
    up_svc_count = 0
    total_svc_count = len(vm.get('services', []))
    
    for svc in vm.get('services', []):
        is_open = check_port(clean_ip, svc['port'])
        if is_open: up_svc_count += 1
        services_status.append({"name": svc['name'], "port": svc['port'], "status": "UP" if is_open else "DOWN"})
        
    return {
        "name": vm_name, "ip": clean_ip, "ping": is_pinging, "is_docker": is_docker,
        "cpu": cpu, "mem": mem, "disk": disk, "dock_run": dock_run, "dock_err": dock_err, 
        "uptime": uptime, "logs": logs,
        "services": services_status, "up_svc": up_svc_count, "tot_svc": total_svc_count
    }

def main():
    print_header("Чтение конфигурации")
    try:
        with open(CONFIG_FILE, 'r', encoding='utf-8') as f: config = yaml.safe_load(f)
    except:
        print(f"{C_RED}Файл {CONFIG_FILE} не найден!{C_RESET}")
        return

    vms = config.get('vms', [])
    real_vms = [vm for vm in vms if not vm.get('check_only', False)]
    ready_vms = set()
    any_new_vm_started = False

    print_header("Этап 1: Проверка и запуск виртуальных машин")
    for vm in real_vms:
        state = get_vm_state(vm['name'])
        if state == "running":
            print(f"[{C_BLUE}{vm['name']}{C_RESET}] {C_GREEN}Уже работает.{C_RESET}")
        elif state in ["shut off", "shutting down"]:
            print(f"[{C_BLUE}{vm['name']}{C_RESET}] Состояние '{C_YELLOW}{state}{C_RESET}'. Запуск...")
            if start_vm(vm['name']): any_new_vm_started = True
        else:
            print(f"[{C_BLUE}{vm['name']}{C_RESET}] Неизвестное состояние: {state}")

    print_header("Этап 2: Подключение к гостевым агентам")
    for vm in real_vms:
        if wait_for_guest_agent(vm['name']): ready_vms.add(vm['name'])

    if any_new_vm_started:
        print(f"\n{C_CYAN}Ожидание 10 сек для стабилизации сетевого стека ОС...{C_RESET}")
        time.sleep(10)

    print_header("Этап 3: Проверка и настройка сетевых интерфейсов")
    for vm in vms:
        if not vm.get('check_only', False) and vm['name'] in ready_vms:
            configure_network(vm)
            
    if any_new_vm_started:
        animated_progress_bar(DOCKER_WAIT_TIME)
    else:
        print(f"\n{C_CYAN}ВМ уже работали. Пропуск ожидания загрузки Docker.{C_RESET}")
    
    print_header("Этап 4: СБОР МЕТРИК И СКАНИРОВАНИЕ")
    scan_start_time = time.time()
    results = []
    
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as executor:
        futures = [executor.submit(scan_single_vm, vm, ready_vms) for vm in vms]
        for future in concurrent.futures.as_completed(futures):
            results.append(future.result())
            
    scan_end_time = time.time()
    results.sort(key=lambda x: [v['name'] for v in vms].index(x['name']))
    print("\n" + C_BOLD + C_BLUE + "━"*172 + C_RESET)
    header = f"{'УЗЕЛ / КОНТЕЙНЕР':<24} | {'IP АДРЕС':<14} | {'CPU':<5} | {'RAM':<5} | {'DISK':<5} | {'UPTIME':<7} | {'ЛОГИ':<5} | {'DOCKER':<9} | {'PING':<4} | {'СТАТУС СЕРВИСОВ'}"
    print(C_BOLD + header + C_RESET)
    print(C_BLUE + "━"*172 + C_RESET)
    total_nodes = len(vms)
    up_nodes = 0
    total_services = 0
    up_services = 0
    
    for i, res in enumerate(results):
        if res['ping']: up_nodes += 1
        total_services += res['tot_svc']
        up_services += res['up_svc']

        if not res['is_docker']:
            print(C_BLUE + "┠" + "─"*171 + C_RESET)
            name_disp = pad_ansi(res['name'], f"{C_BOLD}{res['name']}{C_RESET}", 24)
        else:
            is_last = True
            if i + 1 < len(results) and results[i+1]['is_docker']:
                is_last = False
            branch = " └──" if is_last else " ├──"
            raw_name = f"{branch} {res['name']}"
            name_disp = pad_ansi(raw_name, f"{C_GRAY}{branch}{C_RESET} {C_CYAN}{res['name']}{C_RESET}", 24)

        p_icon = pad_ansi("UP", f"{C_GREEN}UP{C_RESET}", 4) if res['ping'] else pad_ansi("DOWN", f"{C_RED}DOWN{C_RESET}", 4)
        
        def format_metric(val, is_percent=True):
            if val == "-": return pad_ansi("-", f"{C_GRAY}-{C_RESET}", 5)
            try:
                num = int(val)
                raw = f"{num}%" if is_percent else str(num)
                col = C_GREEN if num < 50 else C_YELLOW if num < 80 else C_RED
                return pad_ansi(raw, f"{col}{raw}{C_RESET}", 5)
            except:
                return pad_ansi(val, val, 5)

        c_cpu  = format_metric(res['cpu'])
        c_mem  = format_metric(res['mem'])
        c_disk = format_metric(res['disk'])
        c_up  = pad_ansi(res['uptime'], f"{C_CYAN}{res['uptime']}{C_RESET}", 7) if res['uptime'] != "-" else pad_ansi("-", f"{C_GRAY}-{C_RESET}", 7)
        c_log = pad_ansi(res['logs'], f"{C_MAGENTA}{res['logs']}{C_RESET}", 5) if res['logs'] != "-" else pad_ansi("-", f"{C_GRAY}-{C_RESET}", 5)

        if res['dock_run'] == "-":
            c_dock = pad_ansi("-", f"{C_GRAY}-{C_RESET}", 9)
        else:
            d_run, d_err = int(res['dock_run']), int(res['dock_err'])
            if d_err > 0:
                raw_dock = f"{d_run} (⚠{d_err})"
                colored_dock = f"{C_CYAN}{d_run}{C_RESET} {C_RED}(⚠{d_err}){C_RESET}"
                c_dock = pad_ansi(raw_dock, colored_dock, 9)
            else:
                c_dock = pad_ansi(str(d_run), f"{C_CYAN}{d_run}{C_RESET}", 9)

        formatted_services = []
        for s in res['services']:
            if s['status'] == 'UP': 
                formatted_services.append(f"{C_GREEN}● {s['name']}({s['port']}){C_RESET}")
            else: 
                formatted_services.append(f"{C_RED}○ {s['name']}({s['port']}){C_RESET}")
    
        if not formatted_services: 
            formatted_services = [f"{C_GRAY}Нет сервисов{C_RESET}"]

        CHUNK_SIZE = 4
        service_chunks = [formatted_services[j:j + CHUNK_SIZE] for j in range(0, len(formatted_services), CHUNK_SIZE)]
        first_line_services = "  ".join(service_chunks[0])
        print(f"{name_disp} | {res['ip']:<14} | {c_cpu} | {c_mem} | {c_disk} | {c_up} | {c_log} | {c_dock} | {p_icon} | {first_line_services}")

        if len(service_chunks) > 1:
            empty_prefix = " " * 24 + " | " + " " * 14 + " | " + " " * 5 + " | " + " " * 5 + " | " + " " * 5 + " | " + " " * 7 + " | " + " " * 5 + " | " + " " * 9 + " | " + " " * 4 + " | "
            for chunk in service_chunks[1:]:
                print(f"{empty_prefix}{'  '.join(chunk)}")
        
    print(C_BLUE + "━"*172 + C_RESET)
    scan_time = round(scan_end_time - scan_start_time, 2)
    print(f"\n{C_BOLD}{C_CYAN}┏━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┓{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}┃{C_RESET} {C_BOLD}СВОДКА ПО ИНФРАСТРУКТУРЕ{C_RESET}                            {C_BOLD}{C_CYAN}┃{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}┣━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┫{C_RESET}")
    node_color = C_GREEN if up_nodes == total_nodes else C_YELLOW
    svc_color = C_GREEN if up_services == total_services else C_RED
    print(f"{C_BOLD}{C_CYAN}┃{C_RESET} Доступность узлов (Ping): {node_color}{up_nodes}/{total_nodes} ({int(up_nodes/total_nodes*100)}%){C_RESET}".ljust(71) + f"{C_BOLD}{C_CYAN}┃{C_RESET}")

    if total_services > 0:
        print(f"{C_BOLD}{C_CYAN}┃{C_RESET} Успешно поднято сервисов: {svc_color}{up_services}/{total_services} ({int(up_services/total_services*100)}%){C_RESET}".ljust(71) + f"{C_BOLD}{C_CYAN}┃{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}┃{C_RESET} {C_GRAY}Скорость сканирования: {scan_time} сек.{C_RESET}".ljust(71) + f"{C_BOLD}{C_CYAN}┃{C_RESET}")
    print(f"{C_BOLD}{C_CYAN}┗━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━┛{C_RESET}\n")

if __name__ == "__main__":
    main()