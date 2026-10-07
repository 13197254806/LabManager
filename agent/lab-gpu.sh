# 由 lab-agent 安装：根据实验室平台上的预约，自动设置可用显卡。
# 安装位置 /etc/profile.d/lab-gpu.sh，并由 /etc/bash.bashrc 引用。
# 真正的限制在设备文件权限上（没预约的卡打不开）；这里的环境变量是双保险。
# 用 UUID 而不是序号：设备权限收紧后 CUDA 只枚举能打开的卡，序号会重新编号，UUID 不会。
_lab_user="${USER:-$(id -un 2>/dev/null)}"
if [ -n "$_lab_user" ]; then
    _lab_file="/etc/lab-gpu/$_lab_user.uuid"
    [ -r "$_lab_file" ] || _lab_file="/etc/lab-gpu/$_lab_user"
    if [ -r "$_lab_file" ]; then
        export CUDA_DEVICE_ORDER=PCI_BUS_ID   # 让 CUDA 的编号与 nvidia-smi 一致
        export CUDA_VISIBLE_DEVICES="$(cat "$_lab_file")"
    fi
fi
unset _lab_user _lab_file
