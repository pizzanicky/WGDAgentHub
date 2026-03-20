import smtplib
from email.mime.text import MIMEText
from email.header import Header


class EmailProvider:
    """
    负责通过 SMTP 发送报告。
    """

    def __init__(self, smtp_server, smtp_port, user, password):
        self.smtp_server = smtp_server
        self.smtp_port = smtp_port
        self.user = user
        self.password = password

    def send_email(self, to_addr, subject, content):
        """
        发送 HTML 格式的邮件内容。
        """
        if not all([self.smtp_server, self.user, self.password]):
            return False, "SMTP 配置不完整"

        try:
            msg = MIMEText(content, "html", "utf-8")
            msg["From"] = self.user
            msg["To"] = to_addr
            msg["Subject"] = Header(subject, "utf-8")

            # 建立 SMTP 连接 (支持 SSL)
            with smtplib.SMTP_SSL(self.smtp_server, self.smtp_port) as server:
                server.login(self.user, self.password)
                server.sendmail(self.user, [to_addr], msg.as_string())

            return True, None
        except Exception as e:
            return False, str(e)
