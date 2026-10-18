import type { ResearchStrategyMetadata } from '@/lib/api'

export const researchCategoryLabels = { smallcap: '小市值', dividend: '红利', trend: '趋势', other: '其他' } as const
export type ResearchCategory = keyof typeof researchCategoryLabels

export function researchCategory(id: string, name: string, metadata?: ResearchStrategyMetadata): ResearchCategory {
  if (metadata?.category) return metadata.category
  const text = `${id} ${name}`.toLowerCase()
  if (/graham|smallcap|小市值/.test(text)) return 'smallcap'
  if (/dividend|红利/.test(text)) return 'dividend'
  if (/zhixing|知行|趋势/.test(text)) return 'trend'
  return 'other'
}
