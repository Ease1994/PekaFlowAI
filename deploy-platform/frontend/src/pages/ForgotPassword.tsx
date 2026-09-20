import { useState } from 'react'
import { Card, Form, Input, Button, Typography, message } from 'antd'
import { MailOutlined, DeploymentUnitOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import { postR } from '@/api/client'
import { resolveDisplayName, usePlatformBranding } from '@/hooks/usePlatformBranding'
import LanguageSwitch from '@/components/LanguageSwitch'
import { useT } from '@/i18n'

const { Title, Text } = Typography

/**
 * 忘记密码。只对有本系统密码且配了真实邮箱的账号发信。
 * 无论账号在不在都显示同一句成功，避免被人用来枚举用户。
 */
export default function ForgotPassword() {
  const [loading, setLoading] = useState(false)
  const [sent, setSent] = useState(false)
  const { data: branding } = usePlatformBranding()
  const t = useT()
  const displayName = resolveDisplayName(branding, t)

  const onFinish = async (values: { username: string }) => {
    setLoading(true)
    try {
      const res = await postR('/auth/forgot-password', {
        username: values.username,
        origin: window.location.origin,
      })
      setSent(true)
      message.success(res.message || t('forgot.sent'))
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
          <Text type="secondary">{t('forgot.subtitle')}</Text>
        </div>
        {sent ? (
          <>
            <Text>{t('forgot.sent')}</Text>
            <div style={{ marginTop: 24, textAlign: 'center' }}>
              <Link to="/login">{t('forgot.back')}</Link>
            </div>
          </>
        ) : (
          <>
            <Form onFinish={onFinish} size="large">
              <Form.Item
                name="username"
                rules={[{ required: true, message: t('forgot.usernameRequired') }]}
              >
                <Input prefix={<MailOutlined />} placeholder={t('forgot.usernamePlaceholder')} />
              </Form.Item>
              <Form.Item>
                <Button type="primary" htmlType="submit" block loading={loading}>
                  {t('forgot.send')}
                </Button>
              </Form.Item>
            </Form>
            <Text type="secondary" style={{ fontSize: 12, display: 'block' }}>
              {t('forgot.hint')}
            </Text>
            <div style={{ marginTop: 16, textAlign: 'center' }}>
              <Link to="/login">{t('forgot.back')}</Link>
            </div>
          </>
        )}
      </Card>
    </div>
  )
}
