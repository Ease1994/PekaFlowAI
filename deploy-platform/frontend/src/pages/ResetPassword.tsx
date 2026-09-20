import { useMemo, useState } from 'react'
import { Card, Form, Input, Button, Typography, message } from 'antd'
import { LockOutlined, DeploymentUnitOutlined } from '@ant-design/icons'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { postR } from '@/api/client'
import { resolveDisplayName, usePlatformBranding } from '@/hooks/usePlatformBranding'
import LanguageSwitch from '@/components/LanguageSwitch'
import { useT } from '@/i18n'

const { Title, Text } = Typography

/**
 * 邮件重置链接落地页。token 在查询串里，一次性，用过即废。
 * 改密会清掉双因子绑定，下次登录重新扫码。
 */
export default function ResetPassword() {
  const [loading, setLoading] = useState(false)
  const [search] = useSearchParams()
  const navigate = useNavigate()
  const token = useMemo(() => (search.get('token') || '').trim(), [search])
  const { data: branding } = usePlatformBranding()
  const t = useT()
  const displayName = resolveDisplayName(branding, t)

  const onFinish = async (values: { password: string }) => {
    if (!token) {
      message.error(t('reset.invalidLink'))
      return
    }
    setLoading(true)
    try {
      const res = await postR('/auth/reset-password', {
        token,
        password: values.password,
      })
      message.success(res.message || t('reset.done'))
      navigate('/login', { replace: true })
    } catch {
      // 错误已由拦截器提示
    } finally {
      setLoading(false)
    }
  }

  return (
    <div className="login-page">
      <div className="login-lang-switch">
        <LanguageSwitch />
      </div>
      <Card className="login-card">
        <div style={{ textAlign: 'center', marginBottom: 24 }}>
          <div style={{ color: '#1677ff' }}>
            <DeploymentUnitOutlined style={{ fontSize: 48 }} />
          </div>
          <Title level={3} style={{ marginTop: 12, marginBottom: 4 }}>
            {displayName}
          </Title>
          <Text type="secondary">{t('reset.subtitle')}</Text>
        </div>
        {!token ? (
          <>
            <Text type="danger">{t('reset.missingToken')}</Text>
            <div style={{ marginTop: 24, textAlign: 'center' }}>
              <Link to="/forgot-password">{t('reset.reapply')}</Link>
            </div>
          </>
        ) : (
          <>
            <Form onFinish={onFinish} size="large">
              <Form.Item
                name="password"
                rules={[
                  { required: true, message: t('reset.newPassword') },
                  { min: 8, message: t('reset.min8') },
                ]}
              >
                <Input.Password prefix={<LockOutlined />} placeholder={t('reset.min8')} />
              </Form.Item>
              <Form.Item
                name="confirm"
                dependencies={['password']}
                rules={[
                  { required: true, message: t('reset.confirmRequired') },
                  ({ getFieldValue }) => ({
                    validator(_, value) {
                      if (!value || getFieldValue('password') === value) {
                        return Promise.resolve()
                      }
                      return Promise.reject(new Error(t('reset.mismatch')))
                    },
                  }),
                ]}
              >
                <Input.Password prefix={<LockOutlined />} placeholder={t('reset.confirmPlaceholder')} />
              </Form.Item>
              <Form.Item>
                <Button type="primary" htmlType="submit" block loading={loading}>
                  {t('reset.submit')}
                </Button>
              </Form.Item>
            </Form>
            <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
              {t('reset.hint')}
            </Text>
          </>
        )}
        <div style={{ marginTop: 16, textAlign: 'center' }}>
          <Link to="/login">{t('reset.back')}</Link>
        </div>
      </Card>
    </div>
  )
}
