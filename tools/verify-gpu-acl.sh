#!/bin/bash
# 实机验证“设备文件 ACL 能否让没预约的显卡对成员不可见 / 不可用”，不依赖 Agent，测完自动还原。
# 在 GPU 服务器上用 root 执行：  sudo bash verify-gpu-acl.sh [成员用户名，默认 sct] [被禁止的显卡序号，默认 1] [python 路径]
# 只对这一张卡的设备文件临时加一条 “lab-users 组禁止” 的 ACL；已经在用这张卡的进程不受影响。
set -u
USER_NAME="${1:-sct}"; IDX="${2:-1}"
[ "$(id -u)" -eq 0 ] || { echo "请用 sudo 执行"; exit 1; }
command -v setfacl >/dev/null || { echo "缺少 acl：apt install acl"; exit 1; }
id "$USER_NAME" >/dev/null 2>&1 || { echo "没有用户 $USER_NAME"; exit 1; }
id -nG "$USER_NAME" | tr ' ' '\n' | grep -qx lab-users || { echo "$USER_NAME 不在 lab-users 组，测试没有意义"; exit 1; }

bus="$(nvidia-smi --query-gpu=index,pci.bus_id --format=csv,noheader | awk -F', *' -v i="$IDX" '$1==i{print tolower($2)}')"
[ -n "$bus" ] || { echo "找不到序号 $IDX 的显卡"; exit 1; }
minor="$(grep -i 'Device Minor' "/proc/driver/nvidia/gpus/${bus#0000}/information" | awk '{print $NF}')"
dev="/dev/nvidia$minor"
echo "GPU$IDX（$bus）→ $dev"
had="$(getfacl -cp "$dev" | grep -c '^group:lab-users:' || true)"
restore() { [ "$had" -eq 0 ] && setfacl -x g:lab-users "$dev"; echo; echo "已还原 $dev："; getfacl -cp "$dev"; }
trap restore EXIT

# 找一个装了 torch 的 python：优先第 3 个参数，否则在成员家目录的 conda 本体和各环境里找
home="$(getent passwd "$USER_NAME" | cut -d: -f6)"
PY="${3:-}"
if [ -z "$PY" ]; then
    for cand in "$home"/anaconda3/envs/*/bin/python "$home"/anaconda3/bin/python "$home"/miniconda3/envs/*/bin/python "$home"/miniconda3/bin/python; do
        if sudo -u "$USER_NAME" "$cand" -c 'import torch' >/dev/null 2>&1; then PY="$cand"; break; fi
    done
fi

as_user() { sudo -u "$USER_NAME" -H env -u CUDA_VISIBLE_DEVICES "$@"; }
probe() {
    echo "--- nvidia-smi -L（$USER_NAME 看到的卡）"
    as_user nvidia-smi -L 2>&1 | sed 's/^/    /'
    echo "--- nvidia-smi 退出码：$(as_user nvidia-smi >/dev/null 2>&1; echo $?)"
    echo "--- PyTorch（$PY）"
    if [ -z "$PY" ]; then echo "    没找到装了 torch 的 python，跳过（可以把 python 路径作为第 3 个参数传入）"; return; fi
    pyprobe
}
pyprobe() {  # 环境变量 CUDA_VISIBLE_DEVICES 由调用方决定（as_user 默认清掉）
    as_user "$PY" - <<'PYEOF' 2>&1 | sed 's/^/    /'
import torch
n = torch.cuda.device_count()
print("torch.cuda.device_count() =", n)
for i in range(n):
    try:
        torch.zeros(1, device="cuda:%d" % i)
        print("cuda:%d %s 可以分配显存" % (i, torch.cuda.get_device_name(i)))
    except Exception as e:
        print("cuda:%d 分配失败：%s" % (i, e))
PYEOF
}

echo; echo "===== 1. 加 ACL 之前 ====="; probe
setfacl -m g:lab-users:--- "$dev"
echo; echo "===== 2. 禁止 lab-users 打开 $dev 之后 ====="; getfacl -cp "$dev"; probe
# 被禁止的卡之外的另一张卡：用 UUID 和序号两种写法设置 CUDA_VISIBLE_DEVICES，看哪种在“序号被重新编号”时仍然正确
other="$(nvidia-smi --query-gpu=index,uuid --format=csv,noheader | awk -F', *' -v i="$IDX" '$1!=i{print $1" "$2; exit}')"
oidx="${other% *}"; ouuid="${other#* }"
if [ -n "$PY" ] && [ -n "$oidx" ]; then
    echo; echo "===== 2b. 只放行 GPU$oidx（GPU$IDX 被禁止），CUDA_VISIBLE_DEVICES 的两种写法 ====="
    for v in "$ouuid" "$oidx"; do
        echo "--- CUDA_VISIBLE_DEVICES=$v"
        as_user() { sudo -u "$USER_NAME" -H env CUDA_VISIBLE_DEVICES="$v" "$@"; }
        pyprobe
    done
    as_user() { sudo -u "$USER_NAME" -H env -u CUDA_VISIBLE_DEVICES "$@"; }
fi
echo; echo "===== 3. 单独给 $USER_NAME 放行（模拟他预约了这张卡）====="
setfacl -m "u:$USER_NAME:rw-" "$dev"; probe
setfacl -x "u:$USER_NAME" "$dev"
