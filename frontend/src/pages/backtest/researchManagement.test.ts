import { expect, it } from 'vitest'
import { researchCategory } from './researchManagement'
it('按稳定ID或名称分组，并优先保留用户分类', () => {
  expect(researchCategory('personal_graham_s0', '策略')).toBe('smallcap')
  expect(researchCategory('x', '红利增强')).toBe('dividend')
  expect(researchCategory('personal_zhixing', '策略')).toBe('trend')
  expect(researchCategory('x', '未知')).toBe('other')
  expect(researchCategory('graham', '策略', { category: 'other' })).toBe('other')
})
