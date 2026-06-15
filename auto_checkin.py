import os
import sys
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests
from loguru import logger
from pydantic import BaseModel, Field

from ext_notification import NotificationService
from logging_utils import configure_logger
from settings import Settings, SettingsError, parse_bool


class Response(BaseModel):
    code: int = Field(..., alias="code", description="返回值")
    msg: str = Field(..., alias="msg", description="提示信息")
    success: Optional[bool] = Field(None, alias="success", description="token有时才有")
    data: Optional[Any] = Field(None, alias="data", description="请求成功才有")


class KurobbsClientException(Exception):
    """Custom exception for Kurobbs client errors."""


class KurobbsClient:
    FIND_ROLE_LIST_API_URL = "https://api.kurobbs.com/gamer/role/default"
    SIGN_URL = "https://api.kurobbs.com/encourage/signIn/v2"
    USER_SIGN_URL = "https://api.kurobbs.com/user/signIn"
    USER_MINE_URL = "https://api.kurobbs.com/user/mineV2"

    def __init__(self, token: str):
        if not token:
            raise KurobbsClientException("TOKEN is required to call Kurobbs APIs.")

        self.token = token
        self.session = requests.Session()
        self.session.headers.update(
            {
                "osversion": "Android",
                "devcode": "2fba3859fe9bfe9099f2696b8648c2c6",
                "countrycode": "CN",
                "ip": "10.0.2.233",
                "model": "2211133C",
                "source": "android",
                "lang": "zh-Hans",
                "version": "1.0.9",
                "versioncode": "1090",
                "token": self.token,
                "content-type": "application/x-www-form-urlencoded; charset=utf-8",
                "accept-encoding": "gzip",
                "user-agent": "okhttp/3.10.0",
            }
        )
        self.result: Dict[str, str] = {}
        self.exceptions: List[Exception] = []

    def _post(self, url: str, data: Dict[str, Any]) -> Response:
        """Make a POST request to the specified URL with the given data."""
        try:
            response = self.session.post(url, data=data, timeout=15)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise KurobbsClientException(f"Request to {url} failed: {exc}") from exc

        try:
            res = Response.model_validate_json(response.content)
        except Exception as exc:  # noqa: BLE001
            raise KurobbsClientException(f"Failed to parse response from {url}") from exc

        logger.debug(
            "POST {} -> code={}, success={}, msg={}",
            url,
            res.code,
            res.success,
            res.msg,
        )
        return res

    def get_mine_info(self, type: int = 1) -> Dict[str, Any]:
        """Get mine info."""
        res = self._post(self.USER_MINE_URL, {"type": type})
        if not res.data:
            raise KurobbsClientException("User info is missing in response.")
        return res.data

    def get_user_game_list(self, user_id: int) -> Dict[str, Any]:
        """Get the list of games for the user."""
        res = self._post(self.FIND_ROLE_LIST_API_URL, {"queryUserId": user_id})
        if not res.data:
            raise KurobbsClientException("User game list is missing in response.")
        return res.data

    def checkin(self):
        """
        Perform check-in for all default roles.
        Each role is signed in individually; failures for one role do not block others.
        """
        mine_info = self.get_mine_info()
        user_game_list = self.get_user_game_list(
            user_id=mine_info.get("mine", {}).get("userId", 0)
        )

        role_list = user_game_list.get("defaultRoleList") or []
        if not role_list:
            raise KurobbsClientException("No default role found for the user.")

        beijing_tz = ZoneInfo("Asia/Shanghai")
        beijing_time = datetime.now(beijing_tz)
        req_month = f"{beijing_time.month:02d}"

        success_roles = []
        failed_roles = []

        for role_info in role_list:
            data = {
                "gameId": role_info.get("gameId", 2),
                "serverId": role_info.get("serverId"),
                "roleId": role_info.get("roleId", 0),
                "userId": role_info.get("userId", 0),
                "reqMonth": req_month,
            }
            role_name = role_info.get("roleName", f"角色ID:{role_info.get('roleId', '?')}")
            try:
                resp = self._post(self.SIGN_URL, data)
                if resp.success:
                    success_roles.append(role_name)
                    logger.info("签到奖励成功 [{}]", role_name)
                else:
                    failed_roles.append(f"{role_name}({resp.msg})")
                    logger.warning("签到奖励失败 [{}]: {}", role_name, resp.msg)
            except KurobbsClientException as e:
                failed_roles.append(f"{role_name}(异常: {e})")
                logger.error("签到奖励异常 [{}]: {}", role_name, e)

        # 汇总结果
        if success_roles:
            self.result["checkin"] = f"签到奖励成功: {', '.join(success_roles)}"
        if failed_roles:
            self.exceptions.append(
                KurobbsClientException(f"签到奖励失败: {'; '.join(failed_roles)}")
            )

    def sign_in(self) -> Response:
        """Perform the community sign-in operation."""
        return self._post(self.USER_SIGN_URL, {"gameId": 2})

    def _process_sign_action(
        self,
        action_name: str,
        action_method: Callable[[], Response],
        success_message: str,
        failure_message: str,
    ):
        """Handle the common logic for a single sign-in action."""
        resp = action_method()
        if resp.success:
            self.result[action_name] = success_message
            logger.info("{} -> {}", action_name, success_message)
        else:
            self.exceptions.append(KurobbsClientException(f"{failure_message}, {resp.msg}"))

    def start(self):
        """Start the sign-in process (multi‑role checkin + community sign‑in)."""
        # 多角色签到，即使基础信息获取失败也继续社区签到
        try:
            self.checkin()
        except KurobbsClientException as e:
            self.exceptions.append(e)
            logger.error(str(e))

        # 社区签到（与角色无关）
        self._process_sign_action(
            action_name="sign_in",
            action_method=self.sign_in,
            success_message="社区签到成功",
            failure_message="社区签到失败",
        )

        self._log()

    @property
    def msg(self) -> str:
        parts = list(self.result.values())
        if parts:
            return "; ".join(parts) + "!"
        return ""

    def _log(self):
        """Log the results and raise a combined exception if any errors occurred."""
        if msg := self.msg:
            logger.info(msg)
        if self.exceptions:
            raise KurobbsClientException("; ".join(map(str, self.exceptions)))


def main():
    # Configure logging as early as possible to avoid leaking secrets in GitHub Actions logs.
    configure_logger(
        debug=parse_bool(os.getenv("DEBUG", "")),
        secrets=[
            os.getenv("TOKEN", ""),
            os.getenv("BARK_DEVICE_KEY", ""),
            os.getenv("BARK_SERVER_URL", ""),
            os.getenv("SERVER3_SEND_KEY", ""),
        ],
    )

    try:
        settings = Settings.load()
    except SettingsError as exc:
        logger.error(str(exc))
        sys.exit(1)

    notifier = NotificationService(settings)

    try:
        kurobbs = KurobbsClient(settings.token)
        kurobbs.start()
        if kurobbs.msg:
            notifier.send(kurobbs.msg)
    except KurobbsClientException as e:
        logger.error(str(e))
        notifier.send(str(e))
        sys.exit(1)
    except Exception as e:  # noqa: BLE001
        logger.exception("An unexpected error occurred: {}", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
    
