import os
import sys
import requests
from typing import List, Dict, Set
from ipaddress import ip_network

class NetlifyDNSSync:
    def __init__(
        self,
        edns_client_subnet: str = "203.66.32.98",
        force_disable_edns: bool = False
    ):
        """初始化Netlify DNS聚合同步工具
        
        Args:
            edns_client_subnet: EDNS客户端子网 (默认 "203.66.32.98")
            force_disable_edns: 强制禁用EDNS (默认 False)
        """
        # Cloudflare 配置
        self.cf_api_token = self._get_env_var("CLOUDFLARE_API_TOKEN")
        self.cf_zone_id = self._get_env_var("CLOUDFLARE_ZONE_ID", required=False)
        self.target_domain = self._get_env_var("TARGET_DOMAIN")
        
        # Netlify 源主机名（聚合这两个域名的A记录）
        self.source_hostnames = [
            "www.netlify.com",
            "apex-loadbalancer.netlify.com"
        ]
        
        # Google DNS 配置
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
        """执行DNS聚合同步流程"""
        print(f"开始聚合Netlify DNS记录到 {self.target_domain}")
        print(f"源域名: {', '.join(self.source_hostnames)}")
        
        # 显示EDNS状态
        if self.use_edns:
            print(f"EDNS已启用 (客户端子网: {self.edns_client_subnet})")
        else:
            print("EDNS已禁用")

        # 自动获取Zone ID（如果未提供）
        if not self.cf_zone_id:
            self.cf_zone_id = self._get_cf_zone_id()
            print(f"自动获取Cloudflare Zone ID: {self.cf_zone_id}")

        # 聚合所有源的A记录
        aggregated_ips = self._aggregate_dns_records()
        print(f"聚合的A记录IP地址: {aggregated_ips}")

        # 同步到Cloudflare
        if aggregated_ips:
            self._sync_to_cloudflare(aggregated_ips)
        else:
            print("警告: 未获取到任何A记录")

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

    def _aggregate_dns_records(self) -> Set[str]:
        """聚合所有源主机名的A记录"""
        all_ips = set()
        
        for hostname in self.source_hostnames:
            try:
                ips = self._query_google_dns(hostname)
                print(f"  {hostname}: {ips}")
                all_ips.update(ips)
            except Exception as e:
                print(f"  警告: 查询 {hostname} 失败: {str(e)}")
                continue
        
        return all_ips

    def _query_google_dns(self, hostname: str) -> List[str]:
        """查询Google DNS API获取A记录"""
        params = {
            "name": hostname,
            "type": "A",
            "edns_client_subnet": self.edns_client_subnet if self.use_edns else None
        }
        
        response = self.session.get(
            self.google_dns_url,
            headers={"Accept": "application/dns-json"},
            params={k: v for k, v in params.items() if v is not None},
            timeout=10
        )
        response.raise_for_status()
        
        # 提取A记录的IP地址
        return [answer["data"] for answer in response.json().get("Answer", [])
                if answer["type"] == 1]  # type=1 表示A记录

    def _sync_to_cloudflare(self, desired_ips: Set[str]):
        """同步记录到Cloudflare"""
        existing_records = self._get_cf_existing_records()
        current_ips = {r["content"] for r in existing_records}
        
        # 删除多余记录
        for record in existing_records:
            if record["content"] not in desired_ips:
                print(f"[删除] A记录: {record['name']} -> {record['content']}")
                self._delete_cf_record(record["id"])
        
        # 添加新记录
        for ip in desired_ips:
            if ip not in current_ips:
                print(f"[添加] A记录: {self.target_domain} -> {ip}")
                self._create_cf_record(ip)
            else:
                print(f"[保持] A记录: {self.target_domain} -> {ip}")

    def _get_cf_existing_records(self) -> List[Dict]:
        """获取Cloudflare现有的A记录"""
        response = self.session.get(
            f"https://api.cloudflare.com/client/v4/zones/{self.cf_zone_id}/dns_records",
            params={
                "name": self.target_domain,
                "type": "A"
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

    def _create_cf_record(self, ip: str):
        """在Cloudflare创建A记录"""
        data = {
            "type": "A",
            "name": self.target_domain,
            "content": ip,
            "ttl": 1,         # 自动TTL
            "proxied": False  # 不经过Cloudflare代理
        }
        response = self.session.post(
            f"https://api.cloudflare.com/client/v4/zones/{self.cf_zone_id}/dns_records",
            json=data
        )
        response.raise_for_status()


if __name__ == "__main__":
    try:
        sync = NetlifyDNSSync()
        sync.run()
        sys.exit(0)
    except Exception as e:
        print(f"[错误] {type(e).__name__}: {str(e)}", file=sys.stderr)
        sys.exit(1)