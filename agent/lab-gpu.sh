# 由 lab-agent 安装：根据实验室平台上的预约，自动设置可用显卡。
# 安装位置 /etc/profile.d/lab-gpu.sh，并由 /etc/bash.bashrc 引用。
_lab_user="${USER:-$(id -un 2>/dev/null)}"
if [ -n "$_lab_user" ] && [ -r "/etc/lab-gpu/$_lab_user" ]; then
    export CUDA_DEVICE_ORDER=PCI_BUS_ID   # 让 CUDA 的编号与 nvidia-smi 一致
    export CUDA_VISIBLE_DEVICES="$(cat "/etc/lab-gpu/$_lab_user")"
fi
unset _lab_user
