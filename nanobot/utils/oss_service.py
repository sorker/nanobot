# -*- coding: utf-8 -*-
"""
OSS对象存储服务
"""
from __future__ import annotations

from typing import Optional, TYPE_CHECKING
from loguru import logger
import alibabacloud_oss_v2 as oss

if TYPE_CHECKING:
    from nanobot.config.schema import OSSConfig


class OSSService:
    """阿里云OSS对象存储服务"""
    
    def __init__(self, oss_config: "OSSConfig | None" = None):
        # 从配置文件获取OSS配置
        if oss_config is None:
            from nanobot.config.loader import load_config
            config = load_config()
            oss_config = config.tools.oss
        
        self.access_key_id = oss_config.access_key_id
        self.access_key_secret = oss_config.access_key_secret
        self.bucket_name = oss_config.bucket_name
        self.endpoint = oss_config.endpoint
        self.domain = oss_config.domain
        self.region = oss_config.region
        
        # 是否使用内网端点
        if oss_config.use_internal is None:
            # 自动判断：如果endpoint包含"-internal"则使用内网
            self.use_internal = "-internal" in self.endpoint
        else:
            self.use_internal = oss_config.use_internal
        
        # 超时配置（秒）
        self.connect_timeout = oss_config.connect_timeout
        self.readwrite_timeout = oss_config.readwrite_timeout
        
        self._client = None
        
        if self.is_enabled():
            self._init_client()
    
    def is_enabled(self) -> bool:
        """检查OSS服务是否已配置启用"""
        return bool(self.access_key_id and self.access_key_secret and self.bucket_name)
    
    def _init_client(self):
        """初始化OSS客户端"""
        try:
            # 使用静态凭证
            credentials_provider = oss.credentials.StaticCredentialsProvider(
                self.access_key_id,
                self.access_key_secret
            )
            
            # 加载配置
            cfg = oss.config.load_default()
            cfg.credentials_provider = credentials_provider
            cfg.region = self.region
            cfg.endpoint = self.endpoint
            
            # 根据配置决定是否使用内网端点
            cfg.use_internal_endpoint = self.use_internal
            
            # 设置超时时间
            cfg.connect_timeout = self.connect_timeout
            cfg.readwrite_timeout = self.readwrite_timeout
            
            # 创建客户端
            self._client = oss.Client(cfg)
            endpoint_type = "内网" if self.use_internal else "公网"
            logger.info(f"OSS客户端初始化成功: bucket={self.bucket_name}, endpoint={self.endpoint} ({endpoint_type}), 连接超时={self.connect_timeout}s, 读写超时={self.readwrite_timeout}s")
        except Exception as e:
            logger.error(f"OSS客户端初始化失败: {e}")
            raise
    
    def upload_file(self, file_path: str, object_key: str) -> Optional[str]:
        """
        上传文件到OSS
        
        Args:
            file_path: 本地文件路径
            object_key: OSS对象键（文件在OSS中的路径）
        
        Returns:
            OSS文件URL，失败返回None
        """
        if not self.is_enabled():
            return None
        
        try:
            # 读取文件内容
            with open(file_path, 'rb') as f:
                file_data = f.read()
            
            # 上传到OSS
            result = self._client.put_object(oss.PutObjectRequest(
                bucket=self.bucket_name,
                key=object_key,
                body=file_data
            ))
            
            logger.info(f"文件上传到OSS成功: {object_key}")
            
            # 返回OSS URL
            return self.get_file_url(object_key)
        except Exception as e:
            logger.error(f"上传文件到OSS失败: {e}")
            return None
    
    def upload_content(self, content: bytes, object_key: str, content_type: Optional[str] = None) -> Optional[str]:
        """
        上传二进制内容到OSS
        
        Args:
            content: 二进制内容
            object_key: OSS对象键
            content_type: 内容类型（可选）
        
        Returns:
            OSS文件URL，失败返回None
        """
        if not self.is_enabled():
            return None
        
        try:
            request = oss.PutObjectRequest(
                bucket=self.bucket_name,
                key=object_key,
                body=content,
            )
            if content_type:
                request.content_type = content_type
            
            self._client.put_object(request)
            logger.info(f"内容上传到OSS成功: {object_key}")
            return self.get_file_url(object_key)
        except Exception as e:
            logger.error(f"上传内容到OSS失败: {e}")
            return None
    
    def upload_text(self, text: str, object_key: str, encoding: str = 'utf-8', content_type: Optional[str] = None) -> Optional[str]:
        """
        上传文本内容到OSS
        
        Args:
            text: 文本内容
            object_key: OSS对象键
            encoding: 编码格式
            content_type: 内容类型（可选），默认根据扩展名自动判断
        
        Returns:
            OSS文件URL，失败返回None
        """
        content = text.encode(encoding)
        
        # 如果没有指定content_type，根据文件扩展名自动判断
        if not content_type:
            content_type = self._guess_content_type(object_key)
        
        return self.upload_content(content, object_key, content_type=content_type)
    
    @staticmethod
    def _guess_content_type(object_key: str) -> str:
        """根据文件扩展名猜测content_type"""
        key_lower = object_key.lower()
        content_type_map = {
            '.html': 'text/html; charset=utf-8',
            '.htm': 'text/html; charset=utf-8',
            '.json': 'application/json; charset=utf-8',
            '.md': 'text/markdown; charset=utf-8',
            '.xml': 'application/xml; charset=utf-8',
            '.css': 'text/css; charset=utf-8',
            '.js': 'application/javascript; charset=utf-8',
            '.csv': 'text/csv; charset=utf-8',
            '.yaml': 'text/yaml; charset=utf-8',
            '.yml': 'text/yaml; charset=utf-8',
            '.txt': 'text/plain; charset=utf-8',
            '.svg': 'image/svg+xml; charset=utf-8',
        }
        for ext, ct in content_type_map.items():
            if key_lower.endswith(ext):
                return ct
        return 'text/plain; charset=utf-8'
    
    def get_file_url(self, object_key: str) -> str:
        """
        获取OSS文件的访问URL
        
        Args:
            object_key: OSS对象键
        
        Returns:
            文件访问URL
        """
        # 确保object_key不以/开头
        if object_key.startswith('/'):
            object_key = object_key[1:]
        # 使用自定义域名
        if self.domain:
            return f"{self.domain}/{object_key}"
        else:
            # 使用默认OSS域名
            return f"https://{self.bucket_name}.{self.endpoint}/{object_key}"
    
    def delete_file(self, object_key: str) -> bool:
        """
        删除OSS文件
        
        Args:
            object_key: OSS对象键
        
        Returns:
            是否删除成功
        """
        if not self.is_enabled():
            return False
        
        try:
            self._client.delete_object(oss.DeleteObjectRequest(
                bucket=self.bucket_name,
                key=object_key
            ))
            logger.info(f"OSS文件删除成功: {object_key}")
            return True
        except Exception as e:
            logger.error(f"删除OSS文件失败: {e}")
            return False
    
    def file_exists(self, object_key: str) -> bool:
        """
        检查OSS文件是否存在
        
        Args:
            object_key: OSS对象键
        
        Returns:
            文件是否存在
        """
        if not self.is_enabled():
            return False
        
        try:
            return self._client.is_object_exist(
                bucket=self.bucket_name,
                key=object_key
            )
        except Exception as e:
            logger.error(f"检查OSS文件是否存在失败: {e}")
            return False


# 全局OSS服务实例
_oss_service = None


def get_oss_service(oss_config: "OSSConfig | None" = None) -> OSSService:
    """获取OSS服务实例（单例模式）"""
    global _oss_service
    if _oss_service is None:
        _oss_service = OSSService(oss_config)
    return _oss_service
