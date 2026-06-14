import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

# 兼容 Python<3.9 & Windows 时区问题
try:
    from zoneinfo import ZoneInfo
except ImportError:
    from backports.zoneinfo import ZoneInfo

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from loguru import logger
from pydantic import BaseModel, Field

from ext_notification import NotificationService
from logging_utils import configure_logger
from settings import Settings, SettingsError, parse_bool

# ===================== 常量抽离 =====================
GAME_ID_DEFAULT = 2
USER_INFO_TYPE = 1
# API 地址
FIND_ROLE_LIST_API_URL = "https://api.kurobbs.com/gamer/role/default"
SIGN_URL = "https://api.kurobbs.com/encourage/signIn/v2"
USER_SIGN_URL = "https://api.kurobbs.com/user/signIn"
USER_MINE_URL = "https://api.kurobbs.com/user/mineV2"
# 请求基础头
BASE_HEADERS = {
    "osversion": "Android",
    "devcode": "2fba3859fe9bfe9099f2696b8648c2c6",
    "countrycode": "CN",
    "ip": "10.0.2.233",
    "model": "2211133C",
    "source": "android",
    "lang": "zh-Hans",
    "version": "1.0.9",
    "versioncode": "1090",
    "content-type": "application/x-www-form-urlencoded; charset=utf-8",
    "accept-encoding": "gzip",
    "user-agent": "okhttp/3.10.0",
}
# 提示文案
MSG_SIGN_SUCCESS = "签到成功"
MSG_SIGN_DONE = "今日已完成签到"
MSG_SIGN_FAIL = "签到失败"


class Response(BaseModel):
    code: int = Field(..., alias="code", description="返回码")
    msg: str = Field(..., alias="msg", description="提示信息")
    success: Optional[bool] = Field(None, alias="success", description="操作结果")
    data: Optional[Any] = Field(None, alias="data", description="返回数据")


class KurobbsClientException(Exception):
    """库洛论坛客户端自定义异常"""


class KurobbsClient:
    def __init__(self, token: str):
        if not token or not token.strip():
            raise KurobbsClientException("Token 不能为空，请检查配置")

        self.token = token.strip()
        self.result: List[str] = []
        self.exceptions: List[Exception] = []

        # 配置 Session + 重试策略
        self.session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # 合并请求头
        headers = BASE_HEADERS.copy()
        headers["token"] = self.token
        self.session.headers.update(headers)

    def _post(self, url: str, data: Dict[str, Any]) -> Response:
        """通用 POST 请求封装"""
        try:
            resp = self.session.post(url, data=data, timeout=15)
            resp.raise_for_status()
        except requests.RequestException as e:
            raise KurobbsClientException(f"请求 {url} 异常：{str(e)}") from e

        try:
            return Response.model_validate_json(resp.content)
        except Exception as e:
            raise KurobbsClientException(f"解析 {url} 响应失败：{str(e)}") from e

    def get_mine_info(self, type: int = USER_INFO_TYPE) -> Dict[str, Any]:
        """获取个人信息"""
        res = self._post(USER_MINE_URL, {"type": type})
        if not res.data:
            raise KurobbsClientException("获取个人信息失败，响应数据为空")
        return res.data

    def get_user_game_list(self, user_id: int) -> Dict[str, Any]:
        """获取游戏角色列表"""
        res = self._post(FIND_ROLE_LIST_API_URL, {"queryUserId": user_id})
        if not res.data:
            raise KurobbsClientException("获取游戏角色列表失败")
        return res.data

    def checkin(self) -> str:
        """游戏角色签到，返回状态文案"""
        mine_info = self.get_mine_info()
        user_id = mine_info.get("mine", {}).get("userId", 0)
        if not user_id:
            raise KurobbsClientException("未获取到用户 ID")

        game_list = self.get_user_game_list(user_id)
        role_list = game_list.get("defaultRoleList", [])
        if not role_list:
            raise KurobbsClientException("未查询到游戏角色，请先在库洛论坛绑定角色")

        role = role_list[0]
        # 北京时间
        tz = ZoneInfo("Asia/Shanghai")
        now = datetime.now(tz)

        post_data = {
            "gameId": role.get("gameId", GAME_ID_DEFAULT),
            "serverId": role.get("serverId", ""),
            "roleId": role.get("roleId", 0),
            "userId": role.get("userId", 0),
            "reqMonth": f"{now.month:02d}",
        }

        res = self._post(SIGN_URL, post_data)
        # 区分 成功 / 已签到 / 失败
        if res.success is True:
            return f"游戏角色{MSG_SIGN_SUCCESS}"
        elif "已签到" in res.msg or "今日已签" in res.msg:
            return f"游戏角色{MSG_SIGN_DONE}"
        else:
            raise KurobbsClientException(f"游戏角色{MSG_SIGN_FAIL}：{res.msg}")

    def sign_in(self) -> str:
        """社区签到，返回状态文案"""
        res = self._post(USER_SIGN_URL, {"gameId": GAME_ID_DEFAULT})
        if res.success is True:
            return f"社区{MSG_SIGN_SUCCESS}"
        elif "已签到" in res.msg or "今日已签" in res.msg:
            return f"社区{MSG_SIGN_DONE}"
        else:
            raise KurobbsClientException(f"社区{MSG_SIGN_FAIL}：{res.msg}")

    def start(self) -> None:
        """执行全部签到流程"""
        # 1. 角色签到
        try:
            ret = self.checkin()
            self.result.append(ret)
            logger.info(ret)
        except KurobbsClientException as e:
            self.exceptions.append(e)
            logger.error(e)

        # 2. 社区签到
        try:
            ret = self.sign_in()
            self.result.append(ret)
            logger.info(ret)
        except KurobbsClientException as e:
            self.exceptions.append(e)
            logger.error(e)

    @property
    def msg(self) -> str:
        return " | ".join(self.result) if self.result else "暂无签到信息"

    def has_error(self) -> bool:
        return len(self.exceptions) > 0


def main():
    # 日志初始化 + 敏感信息脱敏
    debug_mode = parse_bool(os.getenv("DEBUG", ""))
    secrets = [
        os.getenv("TOKEN", ""),
        os.getenv("BARK_DEVICE_KEY", ""),
        os.getenv("BARK_SERVER_URL", ""),
        os.getenv("SERVER3_SEND_KEY", ""),
    ]
    configure_logger(debug=debug_mode, secrets=secrets)

    # 加载配置
    try:
        settings = Settings.load()
    except SettingsError as e:
        logger.error(f"配置加载失败：{e}")
        sys.exit(1)

    notifier = NotificationService(settings)

    try:
        client = KurobbsClient(settings.token)
        client.start()
        notify_text = client.msg

        # 拼接错误信息
        if client.has_error():
            err_text = "；".join(str(e) for e in client.exceptions)
            notify_text += f"【异常】{err_text}"

        notifier.send(notify_text)

        if client.has_error():
            sys.exit(1)

    except Exception as e:
        logger.exception("程序运行异常")
        notifier.send(f"库洛签到程序异常：{str(e)}")
        sys.exit(1)


if __name__ == "__main__":
    main()
