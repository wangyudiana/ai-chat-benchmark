#!/bin/bash
# 同时允许同一 Wi-Fi 下的手机查看结果
cd "$(dirname "$0")"
exec ./启动.command --lan
