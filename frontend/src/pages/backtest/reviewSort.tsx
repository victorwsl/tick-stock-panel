import type { ReactNode } from 'react'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'

export type ReviewSort<K extends PropertyKey> = { key: K; direction: 'asc' | 'desc' }

/** 只排序展示副本；缺失值始终置后，TopK 在排序之后截取。 */
export function sortReviewRows<T>(rows: readonly T[], sort: ReviewSort<keyof T> | null, limit = 0): T[] {
  const sorted = [...rows]
  if (sort) sorted.sort((a, b) => {
    const av = a[sort.key], bv = b[sort.key]
    const missing = (v: unknown) => v == null || v === '' || (typeof v === 'number' && !Number.isFinite(v))
    if (missing(av) || missing(bv)) return Number(missing(av)) - Number(missing(bv))
    const cmp = typeof av === 'number' && typeof bv === 'number'
      ? av - bv : String(av).localeCompare(String(bv), 'zh-CN', { numeric: true })
    return sort.direction === 'asc' ? cmp : -cmp
  })
  return limit > 0 ? sorted.slice(0, limit) : sorted
}

export function ReviewSortHeader<K extends string>({ column, sort, onSort, children, align = 'right' }: {
  column: K
  sort: ReviewSort<K> | null
  onSort: (key: K) => void
  children: ReactNode
  align?: 'left' | 'right'
}) {
  const active = sort?.key === column
  const Icon = !active ? ArrowUpDown : sort.direction === 'asc' ? ArrowUp : ArrowDown
  return <th className={`px-4 py-2.5 font-medium whitespace-nowrap ${align === 'left' ? 'text-left' : 'text-right'}`}
    aria-sort={active ? (sort.direction === 'asc' ? 'ascending' : 'descending') : 'none'}>
    <button type="button" onClick={() => onSort(column)}
      className={`inline-flex items-center gap-1 rounded transition-colors hover:text-accent focus-visible:outline focus-visible:outline-2 focus-visible:outline-accent ${active ? 'text-accent' : ''}`}
      title={`按${typeof children === 'string' ? children : '此列'}${active && sort.direction === 'desc' ? '升序' : '降序'}排列`}>
      {children}<Icon className="h-3 w-3 shrink-0" aria-hidden="true" />
    </button>
  </th>
}
