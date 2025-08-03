#!/usr/bin/env python3
import os
import sys
import requests
import concurrent.futures
from typing import List, Dict, Optional, Tuple, Set
from ipaddress import ip_network, IPv6Address, AddressValueError
import time
import warnings
from datetime import datetime

# 禁用SSL证书验证警告
warnings.filterwarnings("ignore", message="Unverified HTTPS request")

class HybridDNSSync:
    def __init__(
        self,
        edns_client_subnet: str = "104.28.244.152",
        force_disable_edns: bool = False,
        test_path: str = "/",
        top_n: int = 10,
        accepted_status_codes: Set[int] = {404,200},
        https_port: int = 443,
        host_header: str = "update.greasyfork.org.cn",
        user_agent: str = "Mozilla/5.0 (Linux; Android 16; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Mobile Safari/537.36"
    ):
        """初始化混合DNS同步工具
        
        Args:
            edns_client_subnet: EDNS客户端子网
            force_disable_edns: 是否强制禁用EDNS
            test_path: HTTPS测试路径
            top_n: 优选IP数量
            accepted_status_codes: 可接受的状态码
            https_port: HTTPS端口
            host_header: 强制设置的Host头
            user_agent: 自定义User-Agent
        """
        # Cloudflare配置
        self.cf_api_token = self._get_env_var("CLOUDFLARE_API_TOKEN")
        self.cf_zone_id = self._get_env_var("CLOUDFLARE_ZONE_ID", required=False)
        self.target_domain = self._get_env_var("TARGET_DOMAIN")
        
        # DNS配置
        self.source_hostname = self._get_env_var("SOURCE_HOSTNAME", default="4yvjzrwg.litecdncname.com")
        self.google_dns_url = "https://dns.google/resolve"

        # 测试配置
        self.test_path = test_path
        self.top_n = top_n
        self.accepted_status_codes = accepted_status_codes
        self.https_port = https_port
        self.host_header = host_header
        self.user_agent = user_agent
        self.timeout = 5
        self.max_workers = 20
        self.debug = os.getenv("DEBUG", "").lower() in ("1", "true", "yes")

        # EDNS配置
        self.use_edns = not force_disable_edns
        if self.use_edns:
            self._validate_edns_subnet(edns_client_subnet)
            self.edns_client_subnet = edns_client_subnet
        else:
            self.edns_client_subnet = None

        # 初始化会话
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
        print(f"[{datetime.now()}] 开始同步 {self.target_domain} -> {self.source_hostname}")
        print(f"配置参数: top_n={self.top_n}, 端口={self.https_port}, 路径='{self.test_path}'")
        print(f"请求设置: Host={self.host_header}, UA={self.user_agent[:50]}...")
        
        if self.use_edns:
            print(f"EDNS已启用 (客户端子网: {self.edns_client_subnet})")
        else:
            print("EDNS已禁用")

        # 自动获取Zone ID
        if not self.cf_zone_id:
            self.cf_zone_id = self._get_cf_zone_id()
            print(f"自动获取Cloudflare Zone ID: {self.cf_zone_id}")

        # 获取DNS记录
        source_records = self._get_google_dns_records()
        print(f"\n获取到DNS记录: A={len(source_records['A'])}个, AAAA={len(source_records['AAAA'])}个")

        # 测试并优选IP
        optimal_ips = self._test_and_select_optimal_ips(source_records)
        print(f"\n最终优选IP: A={optimal_ips['A']}, AAAA={optimal_ips['AAAA']}")

        # 同步到Cloudflare
        for record_type in ["A", "AAAA"]:
            if optimal_ips[record_type]:
                self._sync_to_cloudflare(record_type, optimal_ips[record_type])

        print(f"\n[{datetime.now()}] 同步完成")

    def _test_and_select_optimal_ips(self, source_records: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """测试HTTPS访问并优选IP"""
        optimal_ips = {"A": [], "AAAA": []}
        
        for record_type in ["A", "AAAA"]:
            ips = source_records[record_type]
            if not ips:
                print(f"\n{record_type}记录: 无可用IP")
                continue
                
            print(f"\n{record_type}记录测试开始 (共{len(ips)}个IP)...")
            tested_ips = self._test_ips_https_access(ips)
            
            # 筛选有效IP
            accepted_ips = {
                ip: (status, latency) 
                for ip, (status, latency) in tested_ips.items()
                if status in self.accepted_status_codes
            }
            
            if not accepted_ips:
                print(f"警告: 没有{record_type}记录返回可接受的状态码")
                continue
            
            # 排序: 先状态码升序(200优先)，后延迟升序
            sorted_ips = sorted(
                accepted_ips.items(),
                key=lambda x: (x[1][0], x[1][1])
            )
            
            # 选择最优IP
            selected = [ip for ip, (status, _) in sorted_ips[:self.top_n]]
            optimal_ips[record_type] = selected
            
            # 打印结果
            print(f"\n{record_type}记录测试结果 (共{len(accepted_ips)}个有效IP):")
            for i, (ip, (status, latency)) in enumerate(sorted_ips[:10], 1):  # 只显示前10个
                print(f"  {i:2d}. {ip:<39} 状态码={status:<3} 延迟={latency:7.2f}ms")
            print(f"\n优选{min(self.top_n, len(selected))}个IP: {selected}")
            
        return optimal_ips

    def _test_ips_https_access(self, ips: List[str]) -> Dict[str, Tuple[int, float]]:
        """并发测试HTTPS访问"""
        results = {}
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_ip = {
                executor.submit(self._test_single_ip_https, ip): ip
                for ip in ips
            }
            
            for future in concurrent.futures.as_completed(future_to_ip):
                ip = future_to_ip[future]
                try:
                    status, latency = future.result()
                    results[ip] = (status, latency)
                    if self.debug:
                        status_str = "✓" if status in self.accepted_status_codes else "✗"
                        print(f"  {status_str} {ip:<39} {status:<3} {latency:7.2f}ms")
                except Exception as e:
                    print(f"测试{ip}时出错: {str(e)}")
                    results[ip] = (0, float('inf'))
        
        return results

    def _test_single_ip_https(self, ip: str) -> Tuple[int, float]:
        """测试单个IP的HTTPS访问"""
        url = self._format_https_url(ip)
        headers = {
            "Host": self.host_header,
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "en-US,en;q=0.9",
            "Connection": "close"
        }
        
        try:
            start_time = time.time()
            response = requests.get(
                url,
                headers=headers,
                verify=False,
                timeout=self.timeout,
                allow_redirects=False
            )
            latency = (time.time() - start_time) * 1000
            
            if self.debug and response.status_code in self.accepted_status_codes:
                server = response.headers.get("Server", "Unknown")
                print(f"    {ip} → Server: {server}, Size: {len(response.content)} bytes")
            
            return response.status_code, latency
        except requests.RequestException as e:
            if self.debug:
                print(f"    {ip} 失败: {type(e).__name__}")
            return 0, float('inf')

    def _format_https_url(self, ip: str) -> str:
        """格式化HTTPS URL（自动处理IPv6）"""
        try:
            if isinstance(IPv6Address(ip), IPv6Address):
                return f"https://[{ip}]:{self.https_port}{self.test_path}"
        except AddressValueError:
            pass
        return f"https://{ip}:{self.https_port}{self.test_path}"

    def _get_cf_zone_id(self) -> str:
        """获取Cloudflare Zone ID"""
        domain_parts = self.target_domain.split(".")
        base_domain = ".".join(domain_parts[-2:])
        
        response = self.session.get(
            "https://api.cloudflare.com/client/v4/zones",
            params={"name": base_domain}
        )
        response.raise_for_status()
        
        zones = response.json()["result"]
        if not zones:
            raise ValueError(f"找不到域名 {base_domain} 对应的Zone")
        
        return zones[0]["id"]

    def _get_google_dns_records(self) -> Dict[str, List[str]]:
        """从Google DNS查询记录"""
        try:
            return {
                "A": self._query_google_dns("A"),
                "AAAA": self._query_google_dns("AAAA")
            }
        except Exception as e:
            raise RuntimeError(f"Google DNS查询失败: {str(e)}")

    def _query_google_dns(self, record_type: str) -> List[str]:
        """查询DNS记录"""
        params = {
            "name": self.source_hostname,
            "type": record_type,
            "edns_client_subnet": self.edns_client_subnet if self.use_edns else None
        }
        
        response = requests.get(
            self.google_dns_url,
            headers={"Accept": "application/dns-json"},
            params={k: v for k, v in params.items() if v is not None},
            timeout=10
        )
        response.raise_for_status()
        
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
        """获取现有记录"""
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
        """删除记录"""
        response = self.session.delete(
            f"https://api.cloudflare.com/client/v4/zones/{self.cf_zone_id}/dns_records/{record_id}"
        )
        response.raise_for_status()

    def _create_cf_record(self, record_type: str, ip: str):
        """创建记录"""
        data = {
            "type": record_type,
            "name": self.target_domain,
            "content": ip,
            "ttl": 1,
            "proxied": False
        }
        response = self.session.post(
            f"https://api.cloudflare.com/client/v4/zones/{self.cf_zone_id}/dns_records",
            json=data
        )
        response.raise_for_status()


if __name__ == "__main__":
    try:
        # 示例用法（实际参数从环境变量获取）
        sync = HybridDNSSync()
        sync.run()
        sys.exit(0)
    except Exception as e:
        print(f"\n[错误] {type(e).__name__}: {str(e)}", file=sys.stderr)
        sys.exit(1)