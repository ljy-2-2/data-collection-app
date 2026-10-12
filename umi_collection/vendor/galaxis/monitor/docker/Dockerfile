# Galaxis Monitor 桥接服务镜像
# 基于采集栈核心镜像(含 ROS 2 环境),补齐 Web 依赖后运行桥接。
FROM galaxis-core:latest

RUN pip3 install --no-cache-dir \
    fastapi "uvicorn[standard]" pyyaml pillow

COPY monitor/ /app/monitor/

WORKDIR /app/monitor/bridge

ENTRYPOINT ["/root/ros_entrypoint.sh"]
CMD ["python3", "-m", "galaxis_studio_bridge", "--host", "0.0.0.0", "--port", "8770"]
