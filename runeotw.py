import os
import sys
import requests
from typing import List, Dict, Optional
from ipaddress import ip_network

class HybridDNSSync:
    def __init__(
        self,
        edns_client_subnet: str = "203.66.32.98",
        force_disable_edns: bool = False
    ):
        """初始化混合DNS同步工具
        
        Args:
            edns_client_subnet: EDNS客户端子网 (默认 "0.0.0.0/0")
            force_disable_edns: 强制禁用EDNS (默认 False)
        """
        # Cloudflare 配置
        self.cf_api_token = self._get_env_var("CLOUDFLARE_API_TOKEN")
        self.cf_zone_id = self._get_env_var("CLOUDFLARE_ZONE_ID", required=False)
        self.target_domain = self._get_env_var("TARGET_DOMAIN")
        
        # Google DNS 配置
        self.source_hostname = self._get_env_var("SOURCE_HOSTNAME", default="edgeone.ai")
        self.google_dns_url = "https://dns.google/resolve"

        # EDNS 配置
        self.use_edns = not force_disable_edns
        if self.use_edns:
            self._validate_edns_subnet(edns_client_subnet)
            self.edns_client_subnet = edns_client_subnet
        else:
            self.edns_client_subnet = None

        # HTTP 客户端配置
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {self.cf_api_token}",
            "Content-Type": "application/json"
        })

    def _get_env_var(self, name: str, required: bool = True, default: str = None) -> str:
        """获取环境变量"""
        value = os.getenv(name, default)
        if required and not value:
            raise ValueError(f"环境变量 {name} 未设置")
        return value

    def _validate_edns_subnet(self, subnet: str):
        """验证EDNS子网格式"""
        try:
            ip_network(subnet)
        except ValueError:
            raise ValueError(f"无效的EDNS客户端子网: {subnet} (示例: '203.0.113.1' 或 '2001:db8::/32')")

    def run(self):
        """执行DNS同步流程"""
        print(f"开始同步 {self.target_domain} -> {self.source_hostname}")
        
        # 显示EDNS状态
        if self.use_edns:
            print(f"EDNS已启用 (客户端子网: {self.edns_client_subnet})")
        else:
            print("EDNS已禁用")

        # 自动获取Zone ID（如果未提供）
        if not self.cf_zone_id:
            self.cf_zone_id = self._get_cf_zone_id()
            print(f"自动获取Cloudflare Zone ID: {self.cf_zone_id}")

        # 从Google DNS获取源记录
        source_records = self._get_google_dns_records()
        print(f"从Google DNS获取的记录: A={source_records['A']})

        # 同步到Cloudflare
        for record_type in ["A"]:
            if source_records[record_type]:
                self._sync_to_cloudflare(record_type, source_records[record_type])

        print("同步完成")

    def _get_cf_zone_id(self) -> str:
        """获取Cloudflare Zone ID"""
        domain_parts = self.target_domain.split(".")
        base_domain = ".".join(domain_parts[-2:])  # 提取主域名
        
        response = self.session.get(
            "https://api.cloudflare.com/client/v4/zones",
            params={"name": base_domain}
        )
        response.raise_for_status()
        
        if not (zones := response.json()["result"]):
            raise ValueError(f"找不到域名 {base_domain} 对应的Zone")
        
        return zones[0]["id"]

    def _get_google_dns_records(self) -> Dict[str, List[str]]:
        """从Google DNS查询记录（支持EDNS）"""
        try:
            return {
                "A": self._query_google_dns("A")
            }
        except Exception as e:
            raise RuntimeError(f"Google DNS查询失败: {str(e)}")

    def _query_google_dns(self, record_type: str) -> List[str]:
        """查询Google DNS API"""
        params = {
            "name": self.source_hostname,
            "type": record_type,
            "edns_client_subnet": self.edns_client_subnet if self.use_edns else None
        }
        
        response = self.session.get(
            self.google_dns_url,
            headers={"Accept": "application/dns-json"},
            params={k: v for k, v in params.items() if v is not None},
            timeout=10
        )
        response.raise_for_status()
        
        # 提取响应中的IP地址
        type_code = 1 if record_type == "A" else 28
        return [answer["data"] for answer in response.json().get("Answer", [])
                if answer["type"] == type_code]

    def _sync_to_cloudflare(self, record_type: str, desired_ips: List[str]):
        """同步记录到Cloudflare"""
        existing_records = self._get_cf_existing_records(record_type)
        current_ips = {r["content"] for r in existing_records}
        
        # 删除多余记录
        for record in existing_records:
            if record["content"] not in desired_ips:
                print(f"[删除] {record_type}记录: {record['name']} -> {record['content']}")
                self._delete_cf_record(record["id"])
        
        # 添加新记录
        for ip in desired_ips:
            if ip not in current_ips:
                print(f"[添加] {record_type}记录: {self.target_domain} -> {ip}")
                self._create_cf_record(record_type, ip)

    def _get_cf_existing_records(self, record_type: str) -> List[Dict]:
        """获取Cloudflare现有记录"""
        response = self.session.get(
            f"https://api.cloudflare.com/client/v4/zones/{self.cf_zone_id}/dns_records",
            params={
                "name": self.target_domain,
                "type": record_type
            }
        )
        response.raise_for_status()
        return response.json()["result"]

    def _delete_cf_record(self, record_id: str):
        """删除Cloudflare记录"""
        response = self.session.delete(
            f"https://api.cloudflare.com/client/v4/zones/{self.cf_zone_id}/dns_records/{record_id}"
        )
        response.raise_for_status()

    def _create_cf_record(self, record_type: str, ip: str):
        """在Cloudflare创建记录"""
        data = {
            "type": record_type,
            "name": self.target_domain,
            "content": ip,
            "ttl": 1,      # 自动TTL
            "proxied": False  # 不经过Cloudflare代理
        }
        response = self.session.post(
            f"https://api.cloudflare.com/client/v4/zones/{self.cf_zone_id}/dns_records",
            json=data
        )
        response.raise_for_status()


if __name__ == "__main__":
    try:
        sync = HybridDNSSync()  

        sync.run()
        sys.exit(0)
    except Exception as e:
        print(f"[错误] {type(e).__name__}: {str(e)}", file=sys.stderr)
        sys.exit(1)