"""
File    : backend/utils/s3_manager.py
Author  : 김지우
Description : AWS S3 데이터 구조 관리 및 선택적 SSE-KMS 보안 처리 관리자
"""

import json
import boto3
import asyncio
import logging
from typing import Optional, Literal
from botocore.exceptions import ClientError
from fastapi import UploadFile
from backend.core.config import settings

logger = logging.getLogger(__name__)

class S3Manager:
    def __init__(self):
        self.s3_client = boto3.client(
            's3',
            aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
            aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
            region_name=settings.AWS_REGION
        )
        self.bucket_name = settings.AWS_S3_BUCKET_NAME
        self.kms_key_id = getattr(settings, 'AWS_KMS_KEY_ID', None)

    # backend/utils/s3_manager.py

    def get_s3_key(
        self,
        data_type: Literal['raw', 'analyzed', 'spec'],
        org_id: str,
        device_id: Optional[str],
        uuid: str,
        domain: str = "",
        extension: str = "json"
    ) -> str:
        """
        지우님의 최종 확정 아키텍처 (temp 제거 버전)
        - spec: org/{org_id}/spec/{domain}/{uuid}.pdf
        - cad:  org/{org_id}/{device_id}/cad/{type}/{uuid}.json
        """
        oid = (org_id or "unknown").strip() or "unknown"
        did = (device_id or "unknown").strip() or "unknown"
        if data_type == "spec":
            # 시방서는 장비(device_id)에 귀속되지 않고 조직(org_id) 레벨에서 공유
            return f"org/{oid}/spec/{domain}/{uuid}.{extension}"
        # 도면 작업 데이터는 기기별 cad 폴더 하위 (standards/* 는 공통 표준 동기화 시 별도 키)
        return f"org/{oid}/{did}/cad/{data_type}/{uuid}.json"

    def _upload_json_sync(self, s3_key: str, data: dict, use_kms: bool = False) -> str:
        """동기 방식의 S3 업로드 (보안 전략 적용)"""
        try:
            upload_kwargs = {
                'Bucket': self.bucket_name,
                'Key': s3_key,
                'Body': json.dumps(data, ensure_ascii=False).encode('utf-8'),
                'ContentType': 'application/json',
                'ServerSideEncryption': 'AES256' 
            }
            
            # 분석 결과(analyzed)나 시방서(spec) 등 보안이 중요한 경우 KMS 적용
            if use_kms and self.kms_key_id:
                upload_kwargs['ServerSideEncryption'] = 'aws:kms'
                upload_kwargs['SSEKMSKeyId'] = self.kms_key_id
                logger.info(f"[S3] KMS Encryption ENFORCED for {s3_key}")

            self.s3_client.put_object(**upload_kwargs)
            return s3_key
            
        except ClientError as e:
            logger.error(f"[S3] Upload Failed for {s3_key}: {e}")
            raise

    async def upload_json_async(self, s3_key: str, data: dict, use_kms: bool = False) -> str:
        return await asyncio.to_thread(self._upload_json_sync, s3_key, data, use_kms)
        
    async def download_json_async(self, s3_key: str) -> dict:
        """S3에서 JSON 데이터를 다운로드 (KMS 복호화는 S3가 자동 처리)"""
        def _download():
            response = self.s3_client.get_object(Bucket=self.bucket_name, Key=s3_key)
            return json.loads(response['Body'].read().decode('utf-8'))
        return await asyncio.to_thread(_download)

    async def delete_object_async(self, s3_key: str) -> None:
        """S3 객체 1개를 삭제합니다. 없는 객체 삭제는 S3가 성공으로 처리합니다."""
        if not self.bucket_name or not s3_key:
            return

        def _delete():
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=s3_key)

        await asyncio.to_thread(_delete)
    
    def _upload_file_sync(self, s3_key: str, file_obj: UploadFile, use_kms: bool = False) -> str:
        """실제 파일을 S3에 업로드하는 동기 함수 (KMS 지원)"""
        upload_kwargs = {
            'Bucket': self.bucket_name,
            'Key': s3_key,
            'Body': file_obj.file,
            'ServerSideEncryption': 'AES256'
        }
        
        if use_kms and self.kms_key_id:
            upload_kwargs['ServerSideEncryption'] = 'aws:kms'
            upload_kwargs['SSEKMSKeyId'] = self.kms_key_id

        self.s3_client.put_object(**upload_kwargs)
        return f"s3://{self.bucket_name}/{s3_key}"

    async def upload_file_async(self, s3_key: str, file_obj: UploadFile, use_kms: bool = False) -> str:
        """비동기 파일 업로드 래퍼"""
        return await asyncio.to_thread(self._upload_file_sync, s3_key, file_obj, use_kms)

    def generate_presigned_url(self, s3_key: str, expiration: int = 600) -> str:
        """RunPod 워커가 PDF를 다운로드할 수 있도록 임시 HTTPS URL 생성 (기본 10분)"""
        return self.s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': self.bucket_name, 'Key': s3_key},
            ExpiresIn=expiration,
        )
