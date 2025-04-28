import requests
import json
from typing import List, Dict, Optional

class CloudflareDNSManager:
    def __init__(self, api_token: str, zone_id: str):
        """
        初始化 Cloudflare DNS 管理器
        
        :param api_token: Cloudflare API 令牌
        :param zone_id: Cloudflare 区域 ID
        """
        self.api_token = api_token
        self.zone_id = zone_id
        self.base_url = "https://api.cloudflare.com/client/v4"
        self.headers = {
            "Authorization": f"Bearer {api_token}",
            "Content-Type": "application/json"
        }

    def get_dns_records(self, record_type: str = None, name: str = None) -> List[Dict]:
        """
        获取 DNS 记录
        
        :param record_type: 记录类型 (A, AAAA, CNAME 等)
        :param name: 记录名称
        :return: DNS 记录列表
        """
        url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
        params = {}
        if record_type:
            params["type"] = record_type
        if name:
            params["name"] = name
            
        response = requests.get(url, headers=self.headers, params=params)
        response.raise_for_status()
        return response.json()["result"]

    def update_dns_record(self, record_id: str, record_type: str, name: str, content: str, ttl: int = 1, proxied: bool = False) -> Dict:
        """
        更新 DNS 记录
        
        :param record_id: 记录 ID
        :param record_type: 记录类型
        :param name: 记录名称
        :param content: 记录内容 (IP 地址)
        :param ttl: TTL 值 (1 表示自动)
        :param proxied: 是否通过 Cloudflare 代理
        :return: 更新后的记录
        """
        url = f"{self.base_url}/zones/{self.zone_id}/dns_records/{record_id}"
        data = {
            "type": record_type,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied
        }
        
        response = requests.put(url, headers=self.headers, json=data)
        response.raise_for_status()
        return response.json()["result"]

    def create_dns_record(self, record_type: str, name: str, content: str, ttl: int = 1, proxied: bool = False) -> Dict:
        """
        创建 DNS 记录
        
        :param record_type: 记录类型
        :param name: 记录名称
        :param content: 记录内容 (IP 地址)
        :param ttl: TTL 值 (1 表示自动)
        :param proxied: 是否通过 Cloudflare 代理
        :return: 新创建的记录
        """
        url = f"{self.base_url}/zones/{self.zone_id}/dns_records"
        data = {
            "type": record_type,
            "name": name,
            "content": content,
            "ttl": ttl,
            "proxied": proxied
        }
        
        response = requests.post(url, headers=self.headers, json=data)
        response.raise_for_status()
        return response.json()["result"]

    def get_netlify_dns_records(self, hostname: str) -> Dict[str, List[str]]:
        """
        获取 Netlify 的 DNS 记录
        
        :param hostname: 主机名 (如 a.netlify.app)
        :return: 包含 A 和 AAAA 记录的字典
        """
        try:
            # 获取 A 记录
            a_records = []
            a_response = requests.get(f"https://dns.google/resolve?name={hostname}&type=A")
            if a_response.status_code == 200:
                a_data = a_response.json()
                if "Answer" in a_data:
                    a_records = [answer["data"] for answer in a_data["Answer"] if answer["type"] == 1]
            
            # 获取 AAAA 记录
            aaaa_records = []
            aaaa_response = requests.get(f"https://dns.google/resolve?name={hostname}&type=AAAA")
            if aaaa_response.status_code == 200:
                aaaa_data = aaaa_response.json()
                if "Answer" in aaaa_data:
                    aaaa_records = [answer["data"] for answer in aaaa_data["Answer"] if answer["type"] == 28]
            
            return {
                "A": a_records,
                "AAAA": aaaa_records
            }
        except Exception as e:
            print(f"获取 Netlify DNS 记录失败: {e}")
            return {"A": [], "AAAA": []}

    def sync_dns_records(self, target_domain: str, source_hostname: str = "a.netlify.app"):
        """
        同步 DNS 记录
        
        :param target_domain: 目标域名 (如 example.com 或 sub.example.com)
        :param source_hostname: 源主机名 (默认为 a.netlify.app)
        """
        # 获取 Netlify 的当前记录
        netlify_records = self.get_netlify_dns_records(source_hostname)
        print(f"从 {source_hostname} 获取的记录: A={netlify_records['A']}, AAAA={netlify_records['AAAA']}")
        
        # 处理 A 记录
        self._sync_record_type(target_domain, "A", netlify_records["A"])
        
        # 处理 AAAA 记录
        self._sync_record_type(target_domain, "AAAA", netlify_records["AAAA"])
        
        print("DNS 记录同步完成")

    def _sync_record_type(self, target_domain: str, record_type: str, source_ips: List[str]):
        """
        同步特定类型的记录
        
        :param target_domain: 目标域名
        :param record_type: 记录类型 (A 或 AAAA)
        :param source_ips: 源 IP 地址列表
        """
        if not source_ips:
            print(f"没有可用的 {record_type} 记录可同步")
            return
            
        # 获取目标域名的现有记录
        existing_records = self.get_dns_records(record_type=record_type, name=target_domain)
        
        # 删除目标域名中不在源 IP 列表中的记录
        for record in existing_records:
            if record["content"] not in source_ips:
                print(f"删除 {record_type} 记录: {record['name']} -> {record['content']}")
                self._delete_dns_record(record["id"])
        
        # 添加源 IP 列表中不存在于目标域名的记录
        existing_ips = {record["content"] for record in existing_records}
        for ip in source_ips:
            if ip not in existing_ips:
                print(f"创建 {record_type} 记录: {target_domain} -> {ip}")
                self.create_dns_record(record_type, target_domain, ip)

    def _delete_dns_record(self, record_id: str) -> bool:
        """
        删除 DNS 记录
        
        :param record_id: 记录 ID
        :return: 是否成功
        """
        url = f"{self.base_url}/zones/{self.zone_id}/dns_records/{record_id}"
        response = requests.delete(url, headers=self.headers)
        response.raise_for_status()
        return response.json()["success"]


def main():


    
    CLOUDFLARE_ZONE_ID = "edb2169f523e048578511bc5c4161807"
    TARGET_DOMAIN = "nf-cdn.dahi.edu.eu.org" 
    
    # 创建 DNS 管理器实例
    dns_manager = CloudflareDNSManager(CLOUDFLARE_API_TOKEN, CLOUDFLARE_ZONE_ID)
    
    # 执行同步
    dns_manager.sync_dns_records(TARGET_DOMAIN)


if __name__ == "__main__":
    main()