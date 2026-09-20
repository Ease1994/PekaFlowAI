/**
 * 调用手册：登录后嵌入 FastAPI 同一份 OpenAPI 的官方 Swagger UI。
 *
 * 按路由 tag 分类（指标大盘、通知中心等），展开后有参数、请求体、返回示例。
 * 不走 Nginx 上的 /docs。规格从已鉴权的 /meta/openapi 取，Try it out 自动带登录 Token。
 */
import { Alert, Spin } from 'antd'
import { useQuery } from '@tanstack/react-query'
import SwaggerUI from 'swagger-ui-react'
import 'swagger-ui-react/swagger-ui.css'
import { get } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import { useT } from '@/i18n'

/**
 * 给 Swagger 发起的请求补上 Bearer。iframe 打 /docs 带不上 localStorage 里的 JWT，
 * 所以规格内嵌进本页，Try it out 也走这里。
 */
function attachLoginToken(req: { headers?: Record<string, string> }) {
  const token = useAuthStore.getState().token
  if (token) {
    req.headers = { ...req.headers, Authorization: `Bearer ${token}` }
  }
  return req
}

export default function ApiHandbook() {
  const t = useT()
  const { data, isLoading, isError } = useQuery({
    queryKey: ['openapi-handbook'],
    queryFn: () => get<Record<string, unknown>>('/meta/openapi'),
    staleTime: 60_000,
  })

  if (isLoading) {
    return (
      <div style={{ textAlign: 'center', padding: 64 }}>
        <Spin />
      </div>
    )
  }
  if (isError || !data) {
    return <Alert type="error" showIcon message={t('handbook.loadFailed')} />
  }

  return (
    <div className="rp-swagger">
      <Alert type="info" showIcon style={{ marginBottom: 12 }} message={t('handbook.authHint')} />
      <SwaggerUI
        spec={data}
        persistAuthorization
        filter
        tryItOutEnabled
        docExpansion="list"
        defaultModelsExpandDepth={1}
        operationsSorter="alpha"
        requestInterceptor={attachLoginToken}
      />
    </div>
  )
}
