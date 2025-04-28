import os
import sys
import requests
from typing import List, Dict

class CloudflareDNSSync:
    def __init__(self):
        self.api_token = self._get_env_var("CLOUDFLARE_API_TOKEN")
        self.zone_id = self._get_env_var("CLOUDFLARE_ZONE_ID", required=False)
        self.target_domain = self._get_env_var("TARGET_DOMAIN")
        self.source_hostname = self._get_env_var("SOURCE_HOSTNAME", default="a.netlify.app")
        
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.headers = {
            "Authorization": f"Bearer {self.api_token}",
            "Content-Type": "application/json"
        }

    def _get_env_var(self, name: str, required: bool = True, default: str = None) -> str:
        """获取环境变量"""
        value = os.getenv(name, default)
        if required and not value:
            raise ValueError(f"环境变量 {name} 未设置")
        return value

    def run(self):
        """执行同步"""
        print(f"开始同步 {self.target_domain} 到 {self.source_hostname} 的 DNS 记录")
        
        # 如果没有提供 Zone ID，自动获取
        if not self.zone_id:
            self.zone_id = self._get_zone_id()
            print(f"自动获取到 Zone ID: {self.zone_id}")

        # 获取源记录
        source_records = self._get_source_records()
        print(f"从 {self.source_hostname} 获取的记录: A={source_records['A']}, AAAA={source_records['AAAA']}")

        # 同步记录
        for record_type in ["A", "AAAA"]:
            if source_records[record_type]:
                self._sync_records(record_type, source_records[record_type])

        print("DNS 记录同步完成")

    def _get_zone_id(self) -> str:
        """获取 Zone ID"""
        domain_parts = self.target_domain.split(".")
        base_domain = ".".join(domain_parts[-2:])  # 获取主域名
        
        response = requests.get(
            f"{self.base_url}/zones",
            headers=self.headers,
            params={"name": base_domain}
        )
        response.raise_for_status()
        zones = response.json()["result"]
        
        if not zones:
            raise ValueError(f"找不到域名 {base_domain} 对应的 Zone")
        
        return zones[0]["id"]

    def _get_source_records(self) -> Dict[str, List[str]]:
        """获取源 DNS 记录"""
        try:
            return {
                "A": self._query_dns("A"),
                "AAAA": self._query_dns("AAAA")
            }
        except Exception as e:
            raise Exception(f"获取源 DNS 记录失败: {str(e)}")

    def _query_dns(self, record_type: str) -> List[str]:
        """查询 DNS 记录"""
        url = "https://cloudflare-dns.com/dns-query"
        params = {
            "name": self.source_hostname,
            "type": record_type
        }
        headers = {"Accept": "application/dns-json"}
        
        response = requests.get(url, headers=headers, params=params)
        response.raise_for_status()
        data = response.json()
        
        type_code = 1 if record_type == "A" else 28
        return [answer["data"] for answer in data.get("Answer", []) 
                if answer["type"] == type_code]

    def _sync_records(self, record_type: str, desired_ips: List[str]):
        """同步特定类型的记录"""
        existing_records = self._get_existing_records(record_type)
        current_ips = {r["content"] for r in existing_records}
        
        # 删除不需要的记录
        for record in existing_records:
            if record["content"] not in desired_ips:
                print(f"删除 {record_type} 记录: {record['name']} -> {record['content']}")
                self._delete_record(record["id"])
        
        # 添加新记录
        for ip in desired_ips:
            if ip not in current_ips:
                print(f"添加 {record_type} 记录: {self.target_domain} -> {ip}")
                self._create_record(record_type, ip)

    def _get_existing_records(self, record_type: str) -> List[Dict]:
        """获取现有记录"""
        response = requests.get(
            f"{self.base_url}/zones/{self.zone_id}/dns_records",
            headers=self.headers,
            params={
                "name": self.target_domain,
                "type": record_type
            }
        )
        response.raise_for_status()
        return response.json()["result"]

    def _delete_record(self, record_id: str):
        """删除记录"""
        response = requests.delete(
            f"{self.base_url}/zones/{self.zone_id}/dns_records/{record_id}",
            headers=self.headers
        )
        response.raise_for_status()

    def _create_record(self, record_type: str, ip: str):
        """创建记录"""
        data = {
            "type": record_type,
            "name": self.target_domain,
            "content": ip,
            "ttl": 1,  # 自动 TTL
            "proxied": False  # 不通过 Cloudflare 代理
        }
        response = requests.post(
            f"{self.base_url}/zones/{self.zone_id}/dns_records",
            headers=self.headers,
            json=data
        )
        response.raise_for_status()


if __name__ == "__main__":
    try:
        sync = CloudflareDNSSync()
        sync.run()
        sys.exit(0)
    except Exception as e:
        print(f"错误: {str(e)}", file=sys.stderr)
        sys.exit(1)