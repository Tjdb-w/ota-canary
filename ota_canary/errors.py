"""业务错误类型。

每个错误都带有一个稳定的 ``code``，CLI 层用它构造错误 JSON 并以非零码退出。
"""


class OtaError(Exception):
    """所有业务错误的基类。"""

    code = "InvalidArgument"

    def __init__(self, message):
        super().__init__(message)
        self.message = message


class DeviceNotFound(OtaError):
    """找不到指定的设备或发布。"""

    code = "DeviceNotFound"


class DeviceExists(OtaError):
    """重复登记设备或重复创建发布。"""

    code = "DeviceExists"


class InvalidArgument(OtaError):
    """参数非法：格式错误、阈值越界、重复或非当前批报告等。"""

    code = "InvalidArgument"


class InvalidState(OtaError):
    """在当前状态下不允许的操作，例如推进非进行中的发布。"""

    code = "InvalidState"
