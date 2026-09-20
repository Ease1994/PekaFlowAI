import { useState } from 'react'
import { Form, Input, Modal, message } from 'antd'
import { MailOutlined } from '@ant-design/icons'
import { put } from '@/api/client'
import { useAuthStore } from '@/stores/auth'
import type { UserInfo } from '@/api/types'
import { useT } from '@/i18n'

/**
 * 本地管理员第一次登录必须留下真实邮箱。
 * 弹窗不能关：丢了密码或 Authenticator 时，邮件是唯一找回入口。
 */
export default function AdminEmailGate() {
  const user = useAuthStore((s) => s.user)
  const setUser = useAuthStore((s) => s.setUser)
  const [form] = Form.useForm()
  const [saving, setSaving] = useState(false)
  const t = useT()
  const open = Boolean(user?.must_set_email)

  const onOk = async () => {
    const values = await form.validateFields()
    setSaving(true)
    try {
      const next = await put<UserInfo>('/auth/email', { email: values.email })
      setUser(next)
      message.success(t('emailGate.saved'))
    } catch {
      // 错误已由拦截器提示
    } finally {
      setSaving(false)
    }
  }

  return (
    <Modal
      title={t('emailGate.title')}
      open={open}
      closable={false}
      maskClosable={false}
      keyboard={false}
      okText={t('emailGate.ok')}
      cancelButtonProps={{ style: { display: 'none' } }}
      confirmLoading={saving}
      onOk={() => void onOk()}
    >
      <p>
        {t('emailGate.body')}
      </p>
      <Form form={form} layout="vertical">
        <Form.Item
          name="email"
          label={t('emailGate.label')}
          rules={[
            { required: true, message: t('emailGate.required') },
            { type: 'email', message: t('emailGate.invalid') },
          ]}
        >
          <Input prefix={<MailOutlined />} placeholder="admin@company.com" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
