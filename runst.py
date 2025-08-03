import os
import sys
import requests
import concurrent.futures
from typing import List, Dict, Optional, Tuple, Set
from ipaddress import ip_network
import time

class HybridDNSSync:
    def __init__(
        self,
        edns_client_subnet: str = "104.28.244.152",
        force_disable_edns: bool = False,
        test_url: str = "http://update.greasyfork.org.cn",
        top_n: int = 6,
        accepted_status_codes: Set[int] = {200, 404}  # 可接受的状态码
    ):
        """初始化混合DNS同步工具
        
        Args:
            edns_client_subnet: EDNS客户端子网 (默认 "0.0.0.0/0")
            force_disable_edns: 强制禁用EDNS (默认 False)
            test_url: 测试HTTP访问的URL (默认 "http://update.greasyfork.org.cn")
            top_n: 优选IP数量 (默认 6)
            accepted_status_codes: 可接受的状态码集合 (默认 {200, 404})
        """
        # Cloudflare 配置
        self.cf_api_token = self._get_env_var("CLOUDFLARE_API_TOKEN")
        self.cf_zone_id = self._get_env_var("CLOUDFLARE_ZONE_ID", required=False)
        self.target_domain = self._get_env_var("TARGET_DOMAIN")
        
        # Google DNS 配置
        self.source_hostname = self._get_env_var("SOURCE_HOSTNAME", default="a.netlify.app")
        self.google_dns_url = "https://dns.google/resolve"

        # HTTP测试配置
        self.test_url = test_url
        self.top_n = top_n
        self.accepted_status_codes = accepted_status_codes
        self.timeout = 5  # HTTP测试超时时间(秒)
        self.max_workers = 20  # 并发测试线程数

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
        print(f"可接受的状态码: {self.accepted_status_codes}")
        
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
        print(f"从Google DNS获取的记录: A={source_records['A']}, AAAA={source_records['AAAA']}")

        # 测试HTTP访问并优选IP
        optimal_ips = self._test_and_select_optimal_ips(source_records)
        print(f"优选后的IP地址: {optimal_ips}")

        # 同步到Cloudflare
        for record_type, ips in optimal_ips.items():
            if ips:
                self._sync_to_cloudflare(record_type, ips)

        print("同步完成")

    def _test_and_select_optimal_ips(self, source_records: Dict[str, List[str]]) -> Dict[str, List[str]]:
        """测试HTTP访问并优选IP"""
        optimal_ips = {"A": [], "AAAA": []}
        
        for record_type in ["A", "AAAA"]:
            ips = source_records[record_type]
            if not ips:
                continue
                
            print(f"\n开始测试{record_type}记录的HTTP访问...")
            tested_ips = self._test_ips_http_access(ips)
            
            # 筛选可接受的IP
            accepted_ips = {
                ip: (status, latency) 
                for ip, (status, latency) in tested_ips.items()
                if status in self.accepted_status_codes
            }
            
            if not accepted_ips:
                print(f"警告: 没有{record_type}记录返回可接受的状态码")
                continue
            
            # 按状态码和响应时间排序
            # 200优先于404，相同状态码按延迟排序
            sorted_ips = sorted(
                accepted_ips.items(),
                key=lambda x: (x[1][0], x[1][1])  # 先按状态码，再按响应时间
            )
            
            # 选择最佳IP
            selected = [ip for ip, (status, _) in sorted_ips[:self.top_n]]
            optimal_ips[record_type] = selected
            
            # 打印测试结果
            print(f"{record_type}记录测试结果:")
            for ip, (status, latency) in sorted_ips:
                print(f"  {ip}: 状态码={status}, 延迟={latency:.2f}ms")
            print(f"优选IP: {selected}")
            
        return optimal_ips

    def _test_ips_http_access(self, ips: List[str]) -> Dict[str, Tuple[int, float]]:
        """并发测试IP的HTTP访问状态码和延迟"""
        results = {}
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            future_to_ip = {
                executor.submit(self._test_single_ip, ip): ip
                for ip in ips
            }
            
            for future in concurrent.futures.as_completed(future_to_ip):
                ip = future_to_ip[future]
                try:
                    status, latency = future.result()
                    results[ip] = (status, latency)
                except Exception as e:
                    print(f"测试{ip}时出错: {str(e)}")
                    results[ip] = (0, float('inf'))  # 标记为失败
        
        return results

    def _test_single_ip(self, ip: str) -> Tuple[int, float]:
        """测试单个IP的HTTP访问"""
        headers = {
            "Host": "update.greasyfork.org.cn",
            "User-Agent": "Mozilla/5.0"
        }
        
        try:
            start_time = time.time()
            response = requests.get(
                self.test_url,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False
            )
            latency = (time.time() - start_time) * 1000  # 转换为毫秒
            
            return response.status_code, latency
        except requests.RequestException as e:
            return 0, float('inf')  # 0表示连接失败

    # ... 保留原有的其他方法不变 ...


if __name__ == "__main__":
    try:
        sync = HybridDNSSync(
            test_url="https://update.greasyfork.org.cn/404",
            top_n=6,
            accepted_status_codes={200, 404}  # 明确指定接受200和404状态码
        )  
        sync.run()
        sys.exit(0)
    except Exception as e:
        print(f"[错误] {type(e).__name__}: {str(e)}", file=sys.stderr)
        sys.exit(1)