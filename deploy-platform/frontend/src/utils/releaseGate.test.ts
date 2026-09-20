import { describe, expect, it } from 'vitest'
import {
  editorApprovalRequired,
  graphManifestDefault,
  graphNeedsManifest,
  manifestInputRequired,
  pipelineNeedsManifest,
} from './releaseGate'
import type { Pipeline, PipelineGraph } from '@/api/types'

function graphWith(plugin: string, manifest?: string): PipelineGraph {
  return {
    pipeline_id: 1,
    pipeline_name: 'msbuild',
    version: 1,
    triggers: [],
    stages: [
      {
        id: 's1',
        name: '发布',
        order: 1,
        jobs: [
          {
            id: '1-1',
            name: 'job',
            agent: 'windows',
            agent_icon: '',
            steps: [
              {
                plugin,
                display_name: plugin,
                icon: '',
                order: 1,
                built_in: false,
                with: manifest != null ? { manifest } : {},
              },
            ],
          },
        ],
      },
    ],
  }
}

describe('graphNeedsManifest', () => {
  it('有提取增量包步骤时要出清单', () => {
    expect(graphNeedsManifest(graphWith('pack-incremental', '${{DEPLOY_MANIFEST}}'))).toBe(true)
  })

  it('Docker 流水线不出清单', () => {
    expect(graphNeedsManifest(graphWith('docker-build'))).toBe(false)
  })
})

describe('graphManifestDefault', () => {
  it('占位清单不预填', () => {
    expect(graphManifestDefault(graphWith('pack-incremental', '${{DEPLOY_MANIFEST}}'))).toBe('')
  })

  it('步骤写死的文件列表预填进弹窗', () => {
    expect(graphManifestDefault(graphWith('pack-incremental', 'bin/*.dll\nAreas/'))).toBe(
      'bin/*.dll\nAreas/',
    )
  })
})

describe('manifestInputRequired', () => {
  it('占位未填必须挡住确认', () => {
    expect(manifestInputRequired(true, '', '')).toBe(true)
    expect(manifestInputRequired(true, '${{DEPLOY_MANIFEST}}', '')).toBe(true)
  })

  it('填了清单或步骤已写死文件就不挡', () => {
    expect(manifestInputRequired(true, '', 'bin/*.dll')).toBe(false)
    expect(manifestInputRequired(true, 'bin/*.dll\nAreas/', '')).toBe(false)
    expect(manifestInputRequired(false, '', '')).toBe(false)
  })
})

describe('pipelineNeedsManifest', () => {
  it('详情优先看 uses_pack_incremental', () => {
    expect(pipelineNeedsManifest({ uses_pack_incremental: true } as Pipeline)).toBe(true)
    expect(
      pipelineNeedsManifest({ uses_pack_incremental: false, uses_deploy_manifest: true } as Pipeline),
    ).toBe(false)
  })
})

describe('editorApprovalRequired', () => {
  it('跟随环境用分组策略，强制/豁免覆盖尚未保存的设置', () => {
    const pipeline = { group_approval_required: true, approval_required: true } as Pipeline
    expect(editorApprovalRequired(pipeline, 'inherit')).toBe(true)
    expect(editorApprovalRequired(pipeline, 'exempt')).toBe(false)
    expect(editorApprovalRequired({ group_approval_required: false } as Pipeline, 'force')).toBe(true)
  })
})
