import asyncio
import websockets
import json
import os
import logging
from datetime import datetime
from urllib.parse import urlparse, parse_qs
import argparse

# ===== 日志模块 =====

class Logger:
    _logger = None

    @classmethod
    def get_logger(cls):
        if cls._logger:
            return cls._logger

        log_dir = os.path.join(os.getcwd(), "logs", datetime.now().strftime("%Y-%m-%d"))
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, "signaling.log")

        logger = logging.getLogger("Signaling")
        logger.setLevel(logging.DEBUG)

        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.DEBUG)
        console_formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        console_handler.setFormatter(console_formatter)

        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setLevel(logging.DEBUG)
        file_formatter = logging.Formatter("[%(asctime)s] %(levelname)s: %(message)s", "%Y-%m-%d %H:%M:%S")
        file_handler.setFormatter(file_formatter)

        logger.addHandler(console_handler)
        logger.addHandler(file_handler)

        cls._logger = logger
        return cls._logger

# 获取日志实例
logger = Logger.get_logger()

# ===== 信令模块 =====

# 保存 {用户名: websocket连接对象}
user_connections = {}

# 锁，保护并发访问
user_lock = asyncio.Lock()

def get_path(websocket):
    """
    兼容 websockets 不同版本
    """
    if hasattr(websocket, "path"):
        return websocket.path
    elif hasattr(websocket, "request"):
        return websocket.request.path
    else:
        raise AttributeError("无法获取 websocket 的 path，websockets库版本不兼容")

async def register_user(websocket):
    try:
        path = get_path(websocket)
        parsed_url = urlparse(path)
        params = parse_qs(parsed_url.query)

        user_list = params.get('user')

        if not user_list:
            await websocket.close()
            logger.warning("连接关闭: 缺少 user 参数")
            return

        username = user_list[0]

        logger.info(f"连接建立: 用户={username}")

        async with user_lock:
            old_ws = user_connections.get(username)
            if old_ws and not old_ws.closed:
                try:
                    await old_ws.close()
                    logger.info(f"踢出旧连接: 用户={username}")
                except Exception as e:
                    logger.error(f"关闭旧连接失败: {e}")

            user_connections[username] = websocket

        # 广播用户加入信令
        join_message = json.dumps({
            "type": "user-join",
            "content": {"user": username}
        })
        logger.info(f"广播用户加入信令: {join_message}")  # 记录用户加入信令
        await broadcast_message(join_message, exclude_username=username)

        # 向新加入的用户发送房间内的所有用户列表（不包括自己）
        room_users_message = json.dumps({
            "type": "room-users",
            "content": {"users": [user for user in user_connections.keys() if user != username]}
        })
        logger.info(f"发送房间用户列表: {room_users_message}")  # 记录房间用户列表
        await websocket.send(room_users_message)

        try:
            async for message in websocket:
                logger.info(f"收到消息: {message}")  # 记录收到的消息
                await handle_message(username, message)
        except websockets.ConnectionClosed:
            pass
        except Exception as e:
            logger.error(f"处理消息异常: {e}")
        finally:
            async with user_lock:
                if user_connections.get(username) == websocket:
                    del user_connections[username]
            logger.info(f"连接关闭: 用户={username}")

            # 广播用户离开信令
            leave_message = json.dumps({
                "type": "user-leave",
                "content": {"user": username}
            })
            logger.info(f"广播用户离开信令: {leave_message}")  # 记录用户离开信令
            await broadcast_message(leave_message)

    except Exception as e:
        logger.error(f"注册用户失败: {e}")

async def handle_message(sender_username, message):
    try:
        data = json.loads(message)
    except json.JSONDecodeError:
        logger.error(f"无法解析JSON消息: {message}")
        return

    to_user = data.get('to')
    if not to_user:
        logger.warning(f"消息缺少'to'字段: {data}")
        return

    async with user_lock:
        if to_user.lower() == "all":
            logger.info(f"群发消息: 发送者={sender_username}, 消息={message}")  # 记录群发消息内容
            tasks = []
            for username, ws in user_connections.items():
                if username != sender_username:
                    tasks.append(asyncio.create_task(safe_send(ws, message, username)))
            if tasks:
                await asyncio.gather(*tasks)
        else:
            to_websocket = user_connections.get(to_user)
            if to_websocket:
                try:
                    await to_websocket.send(message)
                    logger.info(f"转发消息: {sender_username} -> {to_user}, 消息={message}")  # 记录转发消息内容
                except websockets.ConnectionClosed:
                    logger.error(f"目标用户 {to_user} 的连接已关闭")
                    if to_user in user_connections:
                        del user_connections[to_user]
            else:
                # 目标用户不在线，回复发送者用户不存在信令
                user_not_found_message = json.dumps({
                    "type": "user-not-found",
                    "content": {"user": to_user}
                })
                await safe_send(user_connections.get(sender_username), user_not_found_message, sender_username)

async def safe_send(ws, message, username):
    try:
        await ws.send(message)
    except websockets.ConnectionClosed:
        logger.error(f"群发失败: 用户 {username} 的连接已关闭")
        async with user_lock:
            if username in user_connections:
                del user_connections[username]

async def broadcast_message(message, exclude_username=None):
    tasks = []
    async with user_lock:
        for username, ws in user_connections.items():
            if username != exclude_username:
                tasks.append(asyncio.create_task(safe_send(ws, message, username)))
        if tasks:
            await asyncio.gather(*tasks)

async def main(port):
    server = await websockets.serve(
        register_user,
        host="0.0.0.0",
        port=port,
        ping_interval=20,
        ping_timeout=20,
        max_size=None
    )
    logger.info(f"服务器启动成功: ws://0.0.0.0:{port}/socket?user=xxx")
    await server.wait_closed()

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="信令服务器")
    parser.add_argument("-p", "--port", type=int, default=8765, help="监听端口，默认为 8765")
    args = parser.parse_args()
    
    asyncio.run(main(args.port))
