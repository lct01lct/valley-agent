from loguru import logger


class ValleyLogger:
    def __init__(self):
        # 先清理 loguru 默认自带的控制台输出（防止后面重复打印）
        logger.remove()

    def create_logger(self, file_path: str, mini: bool = False):
        if mini:
            log_format = "{message}"
            diagnose = False
        else:
            log_format = "{time} | {level} | {name}:{function}:{line} - {message}"
            diagnose = True

        logger.add(
            file_path,
            filter=lambda record: record["extra"].get("file_target") == file_path,
            encoding="utf-8",
            enqueue=True,
            rotation="50 MB",
            format=log_format,
            diagnose=diagnose,
        )

        return logger.bind(file_target=file_path)


valley_logger = ValleyLogger()
main_logger = valley_logger.create_logger("logs/main.log")
