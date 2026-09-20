import { useEffect } from 'react'
import { Card, Form, Input, Select, Button, message, Divider, Alert, Tag, Upload, Space } from 'antd'
import {
  SaveOutlined,
  DatabaseOutlined,
  ClusterOutlined,
  RedoOutlined,
  InboxOutlined,
  PictureOutlined,
  UploadOutlined,
  DeleteOutlined,
  SafetyCertificateOutlined,
} from '@ant-design/icons'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { del, get, post, postForm, put } from '@/api/client'
import { HEADER_NOTICE_COLORS, isFactoryProductName } from '@/hooks/usePlatformBranding'
import { useT } from '@/i18n'

interface Settings {
  log_storage: string
  es_hosts: string
  es_index: string
  es_username: string
  es_password: string
  task_poll_interval: string
  redis_host: string
  redis_port: string
  redis_password: string
  redis_db: string
  ldap_enabled: string
  ldap_server_uri: string
  ldap_bind_dn: string
  ldap_bind_password: string
  ldap_user_search_base: string
  ldap_user_search_filter: string
  ldap_attr_username: string
  ldap_attr_email: string
  ldap_attr_display_name: string
  ldap_auto_provision: string
  wecom_enabled: string
  wecom_corp_id: string
  wecom_agent_id: string
  wecom_secret: string
  wecom_redirect_uri: string
  wecom_auto_provision: string
  wecom_notify_enabled: string
  wecom_app_base: string
  wecom_callback_token: string
  wecom_encoding_aes_key: string
  emergency_bypass_enabled: string
  artifact_prod_retention_days: string
  smtp_enabled: string
  smtp_host: string
  smtp_port: string
  smtp_user: string
  smtp_password: string
  smtp_from: string
  /** 邮件发件人显示名 */
  smtp_from_name: string
  smtp_ssl: string
  /** 侧栏 / 登录页 / 浏览器标题 */
  platform_display_name: string
  /** 顶栏通知横幅文案，空则不显示 */
  header_notice_text: string
  /** 横幅醒目色 */
  header_notice_color: string
  /** 是否已上传顶栏图（计算字段，不入库） */
  header_has_image?: boolean
  /** 顶栏图公开地址（计算字段） */
  header_image_url?: string
  /** 登录 JWT 有效天数 */
  session_expire_days: string
  /** 是否开启 Authenticator 双因子 */
  totp_2fa_enabled: string
  /** 忘记密码邮件里的站点根地址 */
  public_app_base: string
  /** 审计日志保留天数 */
  audit_retention_days: string
  bootstrap_database?: string
  bootstrap_version?: string
}

/** 出厂显示名；表单空值回填用。旧出厂名在 isFactoryProductName 里兼容。 */
const FACTORY_PRODUCT_NAME = 'PekaFlowAI'

export default function Settings() {
  const t = useT()
  const queryClient = useQueryClient()
  const [form] = Form.useForm()

  const { data: settings } = useQuery({
    queryKey: ['settings'],
    queryFn: () => get<Settings>('/settings'),
  })

  // 数据返回后灌入表单（仅 initialValues 不会在异步加载后刷新）
  useEffect(() => {
    if (settings) {
      const {
        header_has_image: _hasImage,
        header_image_url: _imageUrl,
        bootstrap_database: _db,
        bootstrap_version: _ver,
        ...formValues
      } = settings
      form.setFieldsValue({
        ...formValues,
        es_index: settings.es_index || 'rp-exec-logs',
        artifact_prod_retention_days: settings.artifact_prod_retention_days || '10',
        session_expire_days: settings.session_expire_days || '1',
        totp_2fa_enabled: settings.totp_2fa_enabled || 'false',
        public_app_base: settings.public_app_base || '',
        audit_retention_days: settings.audit_retention_days || '180',
        platform_display_name: isFactoryProductName(settings.platform_display_name)
          ? FACTORY_PRODUCT_NAME
          : settings.platform_display_name,
        header_notice_text: settings.header_notice_text || '',
        header_notice_color: settings.header_notice_color || 'red',
      })
    }
  }, [settings, form])

  const saveMutation = useMutation({
    mutationFn: (values: Record<string, string>) => put('/settings', values),
    onSuccess: () => {
      message.success(t('settings.saved'))
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      queryClient.invalidateQueries({ queryKey: ['platform-branding'] })
    },
  })

  const refreshHeader = () => {
    queryClient.invalidateQueries({ queryKey: ['settings'] })
    queryClient.invalidateQueries({ queryKey: ['platform-branding'] })
  }

  const uploadHeaderImage = useMutation({
    mutationFn: async (file: File) => {
      const formData = new FormData()
      formData.append('file', file)
      return postForm('/settings/header-image', formData)
    },
    onSuccess: () => {
      message.success(t('settings.headerImageUpdated'))
      refreshHeader()
    },
  })

  const clearHeaderImage = useMutation({
    mutationFn: () => del('/settings/header-image'),
    onSuccess: () => {
      message.success(t('settings.headerImageCleared'))
      refreshHeader()
    },
  })

  const ldapTest = useMutation({
    mutationFn: () => post<{ ok: boolean; user_found?: boolean; user?: { username: string; email: string } }>('/account/ldap/test', {}),
    onSuccess: (r) => {
      message.success(r.user_found ? t('settings.ldapFound', { name: r.user?.username || '' }) : t('settings.ldapOk'))
    },
  })

  const handleSave = async () => {
    const values = await form.validateFields()
    if (!values.es_index?.trim()) {
      values.es_index = 'rp-exec-logs'
    }
    saveMutation.mutate(values)
  }

  const displayName = isFactoryProductName(settings?.platform_display_name)
    ? t('layout.productName')
    : (settings?.platform_display_name || t('layout.productName'))
  const noticeTheme = HEADER_NOTICE_COLORS[settings?.header_notice_color as keyof typeof HEADER_NOTICE_COLORS]
  const noticeState = settings?.header_notice_text
    ? (noticeTheme ? t(noticeTheme.label) : settings.header_notice_color)
    : t('settings.off')
  const headerImageState = settings?.header_has_image ? t('settings.tagHeaderOn') : t('settings.tagHeaderOff')
  const onOff = (on: boolean) => (on ? t('settings.on') : t('settings.off'))

  return (
    <div>
      <Alert
        type="info"
        showIcon
        message={t('settings.alertTitle')}
        description={t('settings.alertDesc')}
        style={{ marginBottom: 16 }}
      />

      <Card
        title={t('settings.title')}
      >
        <Form
          form={form}
          layout="vertical"
          initialValues={settings}
          onFinish={handleSave}
        >
          <Divider orientation="left">
            <PictureOutlined /> {t('settings.brandSession')}
          </Divider>
          <Form.Item
            label={t('settings.displayName')}
            name="platform_display_name"
            extra={t('settings.displayNameExtra')}
            rules={[{ required: true, message: t('settings.displayNameRequired') }]}
          >
            <Input placeholder={t('layout.productName')} maxLength={32} style={{ maxWidth: 360 }} />
          </Form.Item>
          <Form.Item
            label={t('settings.headerImage')}
            extra={t('settings.headerImageExtra')}
          >
            <Space align="start" wrap>
              {settings?.header_has_image && settings.header_image_url ? (
                <img
                  src={settings.header_image_url}
                  alt={t('settings.headerImageAlt')}
                  style={{ height: 36, maxWidth: 240, objectFit: 'contain', display: 'block' }}
                />
              ) : (
                <span style={{ color: '#8c8c8c', lineHeight: '32px' }}>{t('settings.headerBlank')}</span>
              )}
              <Upload
                accept="image/png,image/jpeg,image/gif,image/webp,.png,.jpg,.jpeg,.gif,.webp"
                showUploadList={false}
                beforeUpload={(file) => {
                  uploadHeaderImage.mutate(file)
                  return false
                }}
              >
                <Button icon={<UploadOutlined />} loading={uploadHeaderImage.isPending}>
                  {t('settings.uploadImage')}
                </Button>
              </Upload>
              {settings?.header_has_image ? (
                <Button
                  icon={<DeleteOutlined />}
                  loading={clearHeaderImage.isPending}
                  onClick={() => clearHeaderImage.mutate()}
                >
                  {t('settings.clear')}
                </Button>
              ) : null}
            </Space>
          </Form.Item>
          <Form.Item
            label={t('settings.headerNotice')}
            name="header_notice_text"
            extra={t('settings.headerNoticeExtra')}
          >
            <Input.TextArea
              placeholder={t('settings.headerNoticePh')}
              maxLength={200}
              showCount
              autoSize={{ minRows: 2, maxRows: 3 }}
              style={{ maxWidth: 560 }}
            />
          </Form.Item>
          <Form.Item
            label={t('settings.noticeColor')}
            name="header_notice_color"
            extra={t('settings.noticeColorExtra')}
          >
            <Select
              style={{ maxWidth: 240 }}
              options={Object.entries(HEADER_NOTICE_COLORS).map(([value, theme]) => ({
                value,
                label: (
                  <Space size={8}>
                    <span
                      style={{
                        display: 'inline-block',
                        width: 14,
                        height: 14,
                        borderRadius: 3,
                        background: theme.bg,
                      }}
                    />
                    {t(theme.label)}
                  </Space>
                ),
              }))}
            />
          </Form.Item>
          <Form.Item
            label={t('settings.smtpFromName')}
            name="smtp_from_name"
            extra={t('settings.smtpFromNameExtra')}
          >
            <Input placeholder={t('layout.productName')} maxLength={64} style={{ maxWidth: 360 }} />
          </Form.Item>
          <Form.Item
            label={t('settings.sessionExpire')}
            name="session_expire_days"
            extra={t('settings.sessionExpireExtra')}
            rules={[{ required: true, message: t('settings.sessionExpireRequired') }]}
          >
            <Input type="number" min={1} max={30} addonAfter={t('common.days')} placeholder="1" style={{ width: 200 }} />
          </Form.Item>
          <Form.Item
            label={
              <span>
                <SafetyCertificateOutlined /> {t('settings.totpLabel')}
              </span>
            }
            name="totp_2fa_enabled"
            extra={t('settings.totpExtra')}
          >
            <Select
              style={{ maxWidth: 360 }}
              options={[
                { label: t('settings.totpOff'), value: 'false' },
                { label: t('settings.totpOn'), value: 'true' },
              ]}
            />
          </Form.Item>
          <Form.Item
            label={t('settings.publicBase')}
            name="public_app_base"
            extra={t('settings.publicBaseExtra', { env: t('env.prod') })}
          >
            <Input placeholder="https://deploy.example.com" style={{ maxWidth: 360 }} />
          </Form.Item>
          <Form.Item
            label={t('settings.auditDays')}
            name="audit_retention_days"
            extra={t('settings.auditDaysExtra')}
            rules={[{ required: true, message: t('settings.retentionRequired') }]}
          >
            <Input type="number" min={30} max={3650} addonAfter={t('common.days')} placeholder="180" style={{ width: 200 }} />
          </Form.Item>

          <Divider orientation="left">
            <DatabaseOutlined /> {t('settings.logBackend')}
          </Divider>
          <Form.Item
            label={t('settings.buildLogs')}
            extra={t('settings.buildLogsExtra')}
          >
            <Alert
              type="info"
              showIcon
              message={t('settings.logFixed')}
              description={t('settings.logFixedDesc')}
            />
          </Form.Item>

          <Divider orientation="left">
            <ClusterOutlined /> {t('settings.esConfig')}
          </Divider>
          <Form.Item
            label={t('settings.esHosts')}
            name="es_hosts"
            extra={t('settings.esHostsExtra')}
          >
            <Input placeholder="http://elasticsearch:9200" />
          </Form.Item>
          <Form.Item
            label={t('settings.esIndex')}
            name="es_index"
            extra={t('settings.esIndexExtra')}
          >
            <Input placeholder="rp-exec-logs" />
          </Form.Item>
          <div className="rp-field-row">
            <Form.Item label={t('settings.esUser')} name="es_username" style={{ flex: 1 }}>
              <Input placeholder="elastic" />
            </Form.Item>
            <Form.Item label={t('settings.esPassword')} name="es_password" style={{ flex: 1 }}>
              <Input.Password placeholder={t('login.passwordPlaceholder')} />
            </Form.Item>
          </div>

          <Divider orientation="left">{t('settings.agentSchedule')}</Divider>
          <Form.Item label={t('settings.pollInterval')} name="task_poll_interval">
            <Input type="number" placeholder="2" />
          </Form.Item>

          <Divider orientation="left">{t('settings.releaseApproval')}</Divider>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('settings.bypassTitle')}
            description={t('settings.bypassDesc', { env: t('env.prod') })}
          />
          <Form.Item
            label={t('settings.bypassLabel')}
            name="emergency_bypass_enabled"
            extra={t('settings.bypassExtra')}
          >
            <Select
              options={[
                { label: t('settings.bypassOn'), value: 'true' },
                { label: t('settings.bypassOff'), value: 'false' },
              ]}
            />
          </Form.Item>

          <Divider orientation="left">
            <InboxOutlined /> {t('settings.artifacts')}
          </Divider>
          <Form.Item
            label={t('settings.artifactDays', { env: t('env.prod') })}
            name="artifact_prod_retention_days"
            extra={t('settings.artifactExtra', {
              prod: t('env.prod'),
              uat: t('env.uat'),
              staging: t('env.staging'),
              test: t('env.test'),
              dev: t('env.dev'),
            })}
            rules={[{ required: true, message: t('settings.retentionRequired') }]}
          >
            <Input type="number" min={1} max={365} addonAfter={t('common.days')} placeholder="10" style={{ width: 200 }} />
          </Form.Item>

          <Divider orientation="left">
            <RedoOutlined /> {t('settings.redisConfig')}
          </Divider>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('settings.redisAlert')}
          />
          <div className="rp-field-row">
            <Form.Item label={t('settings.redisHost')} name="redis_host" style={{ flex: 2 }}>
              <Input placeholder="localhost" />
            </Form.Item>
            <Form.Item label={t('settings.port')} name="redis_port" style={{ flex: 1 }}>
              <Input placeholder="6379" />
            </Form.Item>
          </div>
          <div className="rp-field-row">
            <Form.Item label={t('settings.passwordOptional')} name="redis_password" style={{ flex: 1 }}>
              <Input.Password placeholder={t('settings.passwordEmptyPh')} />
            </Form.Item>
            <Form.Item label={t('settings.redisDb')} name="redis_db" style={{ flex: 1 }}>
              <Input placeholder="0" />
            </Form.Item>
          </div>

          <Divider orientation="left">{t('settings.ldapConfig')}</Divider>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('settings.ldapTitle')}
            description={t('settings.ldapDesc')}
          />
          <Form.Item label={t('settings.ldapEnable')} name="ldap_enabled">
            <Select
              options={[
                { label: t('settings.ldapOff'), value: 'false' },
                { label: t('settings.on'), value: 'true' },
              ]}
            />
          </Form.Item>
          <Form.Item label={t('settings.ldapAuto')} name="ldap_auto_provision">
            <Select
              options={[
                { label: t('settings.ldapAutoOn'), value: 'true' },
                { label: t('settings.ldapAutoOff'), value: 'false' },
              ]}
            />
          </Form.Item>
          <Form.Item label={t('settings.ldapUri')} name="ldap_server_uri">
            <Input placeholder="ldap://ldap.example.com:389" />
          </Form.Item>
          <div className="rp-field-row">
            <Form.Item label={t('settings.ldapBindDn')} name="ldap_bind_dn" style={{ flex: 1 }}>
              <Input placeholder="cn=ldap-bind,dc=example,dc=com" />
            </Form.Item>
            <Form.Item label={t('settings.ldapBindPassword')} name="ldap_bind_password" style={{ flex: 1 }}>
              <Input.Password placeholder={t('settings.ldapBindPassword')} />
            </Form.Item>
          </div>
          <Form.Item label={t('settings.ldapSearchBase')} name="ldap_user_search_base">
            <Input placeholder="ou=Users,dc=example,dc=com" />
          </Form.Item>
          <Form.Item label={t('settings.ldapFilter')} name="ldap_user_search_filter">
            <Input placeholder="(|(mail={username})(userPrincipalName={username})(sAMAccountName={sam}))" />
          </Form.Item>
          <div className="rp-field-row">
            <Form.Item label={t('settings.ldapAttrUser')} name="ldap_attr_username" style={{ flex: 1 }}>
              <Input placeholder="sAMAccountName" />
            </Form.Item>
            <Form.Item label={t('settings.ldapAttrEmail')} name="ldap_attr_email" style={{ flex: 1 }}>
              <Input placeholder="mail" />
            </Form.Item>
            <Form.Item label={t('settings.ldapAttrName')} name="ldap_attr_display_name" style={{ flex: 1 }}>
              <Input placeholder="displayName" />
            </Form.Item>
          </div>
          <Button style={{ marginBottom: 16 }} onClick={() => ldapTest.mutate()} loading={ldapTest.isPending}>
            {t('settings.ldapTest')}
          </Button>

          <Divider orientation="left">{t('settings.wecomConfig')}</Divider>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('settings.wecomAlert')}
          />
          <Form.Item label={t('settings.wecomEnable')} name="wecom_enabled">
            <Select
              options={[
                { label: t('settings.off'), value: 'false' },
                { label: t('settings.on'), value: 'true' },
              ]}
            />
          </Form.Item>
          <div className="rp-field-row">
            <Form.Item label={t('settings.wecomCorpId')} name="wecom_corp_id" style={{ flex: 2 }}>
              <Input placeholder="ww1234567890abcdef" />
            </Form.Item>
            <Form.Item label={t('settings.wecomAgentId')} name="wecom_agent_id" style={{ flex: 1 }}>
              <Input placeholder="1000002" />
            </Form.Item>
          </div>
          <Form.Item label={t('settings.wecomSecret')} name="wecom_secret">
            <Input.Password placeholder={t('settings.wecomSecretPh')} />
          </Form.Item>
          <Form.Item label={t('settings.wecomRedirect')} name="wecom_redirect_uri">
            <Input placeholder="https://your-domain.com/api/v1/auth/wecom/callback" />
          </Form.Item>
          <Form.Item label={t('settings.wecomAuto')} name="wecom_auto_provision">
            <Select
              options={[
                { label: t('settings.wecomAutoOn'), value: 'true' },
                { label: t('settings.wecomAutoOff'), value: 'false' },
              ]}
            />
          </Form.Item>
          <Form.Item
            label={t('settings.wecomNotify')}
            name="wecom_notify_enabled"
            extra={t('settings.wecomNotifyExtra')}
          >
            <Select
              options={[
                { label: t('settings.off'), value: 'false' },
                { label: t('settings.on'), value: 'true' },
              ]}
            />
          </Form.Item>
          <Form.Item label={t('settings.wecomAppBase')} name="wecom_app_base">
            <Input placeholder={t('settings.wecomAppBasePh')} />
          </Form.Item>
          <div className="rp-field-row">
            <Form.Item label={t('settings.wecomToken')} name="wecom_callback_token" style={{ flex: 1 }}>
              <Input placeholder={t('settings.wecomTokenPh')} />
            </Form.Item>
            <Form.Item label="EncodingAESKey" name="wecom_encoding_aes_key" style={{ flex: 1 }}>
              <Input placeholder={t('settings.wecomAesPh')} />
            </Form.Item>
          </div>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 16 }}
            message={t('settings.wecomCallback')}
          />

          <Divider orientation="left">{t('settings.smtpTitle')}</Divider>
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 12 }}
            message={t('settings.smtpAlert')}
          />
          <Form.Item label={t('settings.smtpEnable')} name="smtp_enabled">
            <Select
              options={[
                { label: t('settings.on'), value: 'true' },
                { label: t('settings.off'), value: 'false' },
              ]}
            />
          </Form.Item>
          <div className="rp-field-row">
            <Form.Item label={t('settings.smtpHost')} name="smtp_host" style={{ flex: 2 }}>
              <Input placeholder="smtp.exmail.qq.com" />
            </Form.Item>
            <Form.Item label={t('settings.port')} name="smtp_port" style={{ flex: 1 }}>
              <Input placeholder="465" />
            </Form.Item>
            <Form.Item label="SSL" name="smtp_ssl" style={{ flex: 1 }}>
              <Select
                options={[
                  { label: t('settings.sslOn'), value: 'true' },
                  { label: t('settings.sslOff'), value: 'false' },
                ]}
              />
            </Form.Item>
          </div>
          <div className="rp-field-row">
            <Form.Item label={t('settings.smtpUser')} name="smtp_user" style={{ flex: 1 }}>
              <Input placeholder="mailer@example.com" />
            </Form.Item>
            <Form.Item label={t('settings.smtpFrom')} name="smtp_from" style={{ flex: 1 }}>
              <Input placeholder="mailer@example.com" />
            </Form.Item>
          </div>
          <Form.Item label={t('settings.smtpPassword')} name="smtp_password">
            <Input.Password placeholder={t('settings.smtpPasswordPh')} />
          </Form.Item>

          <Button
            type="primary"
            icon={<SaveOutlined />}
            htmlType="submit"
            loading={saveMutation.isPending}
          >
            {t('settings.saveConfig')}
          </Button>
        </Form>
      </Card>

      <Card title={t('settings.current')} style={{ marginTop: 16 }}>
        {settings ? (
          <div>
            <Tag color="green">{t('settings.tagLogs')}</Tag>
            <Tag>{t('settings.tagEs', { hosts: settings.es_hosts })}</Tag>
            <Tag>{t('settings.tagIndex', { index: (settings.es_index || 'rp-exec-logs').replace(/-\d{4}-\d{2}-\d{2}$/, '') })}</Tag>
            <Tag>{t('settings.tagAiLogs')}</Tag>
            <Tag>{t('settings.tagDb', { db: settings.bootstrap_database || '—' })}</Tag>
            <Tag>{t('settings.tagVersion', { ver: settings.bootstrap_version || '—' })}</Tag>
            <Tag>{t('settings.tagName', { name: displayName })}</Tag>
            <Tag>{t('settings.tagHeaderImage', { state: headerImageState })}</Tag>
            <Tag>{t('settings.tagNotice', { state: noticeState })}</Tag>
            <Tag>{t('settings.tagSession', { n: settings.session_expire_days || 1, unit: t('common.days') })}</Tag>
            <Tag color={settings.totp_2fa_enabled === 'true' ? 'green' : 'default'}>
              {t('settings.tagTotp', { state: onOff(settings.totp_2fa_enabled === 'true') })}
            </Tag>
            <Tag>{t('settings.tagAudit', { n: settings.audit_retention_days || 180, unit: t('common.days') })}</Tag>
            <Tag>{t('settings.tagPoll', { n: settings.task_poll_interval })}</Tag>
            <Tag>{t('settings.tagArtifact', { env: t('env.prod'), n: settings.artifact_prod_retention_days || 10, unit: t('common.days') })}</Tag>
            <Tag>{t('settings.tagRedis', { value: `${settings.redis_host}:${settings.redis_port}/${settings.redis_db}` })}</Tag>
            <Tag color={settings.smtp_enabled === 'true' ? 'green' : 'default'}>
              {t('settings.tagMail', { state: settings.smtp_enabled === 'true' ? settings.smtp_host : t('settings.off') })}
            </Tag>
          </div>
        ) : (
          t('settings.loading')
        )}
      </Card>
    </div>
  )
}
