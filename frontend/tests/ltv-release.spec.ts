import { expect, test } from '@playwright/test'

/**
 * LTV 校准发布 + 留存迟到更正 全链路 UI 验收：
 * 曲线版本/采用登记 → 明细生成 → 发布 → 更正阻断 → 完成更正 → 第二版发布 → 复算 → 回滚。
 */
test.describe('LTV校准发布与留存更正闭环', () => {
  test('留存页：曲线版本、采用记录、迟到更正全流程', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', e => errors.push(e.message))
    page.on('response', r => { if (r.url().includes('/api/') && r.status() >= 400) errors.push(`${r.status()} ${r.url()}`) })

    await page.goto('/retention_calc')
    await expect(page.getByTestId('kpi-card').first()).toBeVisible()

    // 发起计算，固化曲线版本
    await page.getByTestId('run-analysis').click()
    await expect(page.getByTestId('curve-table tbody tr').first()).toBeVisible()
    const firstCurveRow = page.getByTestId('curve-table').locator('tbody tr').first()
    await expect(firstCurveRow).toContainText('RC-001-curve-1')
    await expect(page.getByTestId('retention-chart .analysis-bar').first()).toBeVisible()

    // 登记迟到更正
    await page.getByTestId('correction-reason').fill('E2E：基准群入组事件迟到')
    await page.getByTestId('open-correction').click()
    const correctionRow = page.getByTestId('correction-table').locator('tbody tr').first()
    await expect(correctionRow).toHaveAttribute('data-status', '更正中')
    // 曲线行展示阻断标记
    await expect(page.getByTestId('curve-table')).toContainText('更正阻断中')

    const correctionId = await correctionRow.locator('td').first().innerText()

    // 完成更正 -> 产生替代曲线
    await page.getByTestId(`complete-${correctionId}`).click()
    await expect(page.getByTestId('correction-table').locator('tbody tr').first())
      .toHaveAttribute('data-status', '已完成')
    await expect(page.getByTestId('curve-table')).toContainText('RC-001-curve-2')

    expect(errors).toEqual([])
  })

  test('LTV页：明细生成、发布阻断、两版本发布、复算与回滚', async ({ page }) => {
    const errors: string[] = []
    page.on('pageerror', e => errors.push(e.message))
    page.on('response', r => {
      // 更正阻断场景预期返回 400，属业务校验而非缺陷
      if (r.url().includes('/api/') && r.status() >= 400 && !r.url().includes('/forecast-details')) {
        errors.push(`${r.status()} ${r.url()}`)
      }
    })

    await page.goto('/lifetime_value')
    await expect(page.getByTestId('kpi-card').first()).toBeVisible()

    // 生成 v1 明细
    await page.getByTestId('run-analysis').click()
    const detailRows = page.getByTestId('detail-table').locator('tbody tr[data-testid^="detail-row"]')
    await expect(detailRows.first()).toBeVisible()
    const v1DetailId = await detailRows.first().getByRole('cell').first().innerText()
    await expect(page.getByTestId(`detail-row-${v1DetailId}`)).toHaveAttribute('data-status', '待发布')
    // 明细内嵌入组批次与曲线指纹
    await expect(page.getByTestId(`detail-row-${v1DetailId}`)).toContainText('RC-001-curve')

    // 发布 v1
    await page.getByTestId(`publish-${v1DetailId}`).click()
    await expect(page.getByTestId(`detail-row-${v1DetailId}`)).toHaveAttribute('data-status', '已发布')
    const releaseRows = page.getByTestId('release-table').locator('tbody tr[data-testid^="release-row"]')
    await expect(releaseRows.first()).toBeVisible()
    const v1ReleaseId = await releaseRows.first().getByRole('cell').first().innerText()
    await expect(page.getByTestId(`release-row-${v1ReleaseId}`)).toHaveAttribute('data-status', '已发布')

    // 查看逐用户估值弹窗
    await page.getByTestId(`detail-row-${v1DetailId}`).getByRole('button', { name: '查看估值' }).click()
    await expect(page.getByTestId('detail-modal')).toBeVisible()
    await expect(page.getByTestId('detail-modal').locator('tbody tr').first()).toBeVisible()
    await page.getByTestId('detail-modal').getByRole('button', { name: '关闭' }).click()

    // 复算核对：指纹一致
    await page.getByTestId(`recompute-${v1DetailId}`).click()
    await expect(page.locator('body')).toContainText('指纹一致')

    // 留存侧登记迟到更正，制造阻断
    await page.request.post('/api/retention-calc/RC-001/corrections', {
      data: { reason: 'E2E：发布周期内的迟到更正' },
    })
    // 再生成明细应被后端拒绝，页面出现错误提示且不新增明细
    const detailCountBefore = await detailRows.count()
    await page.getByTestId('run-analysis').click()
    await expect(page.locator('.error-text')).toContainText('留存更正未完成')
    expect(await detailRows.count()).toBe(detailCountBefore)

    // 通过 API 完成更正后，页面生成并发布 v2
    const corrections = await page.request.get('/api/retention-calc/corrections').then(r => r.json())
    const openFix = corrections.items.find((c: any) => c.status === '更正中')
    await page.request.post(`/api/retention-calc/corrections/${openFix.correction_id}/complete`)
    await page.getByTestId('refresh').click()

    await page.getByTestId('run-analysis').click()
    const detailCountAfter = await detailRows.count()
    expect(detailCountAfter).toBe(detailCountBefore + 1)
    const v2DetailRow = detailRows.last()
    const v2DetailId = await v2DetailRow.getByRole('cell').first().innerText()
    expect(v2DetailId).not.toBe(v1DetailId)
    await page.getByTestId(`publish-${v2DetailId}`).click()

    // 两版本发布单：v1 失效、v2 生效，替换关系可见
    await expect(page.getByTestId(`release-row-${v1ReleaseId}`)).toHaveAttribute('data-status', '已失效')
    const v2ReleaseRow = page.getByTestId('release-table').locator('tbody tr').last()
    await expect(v2ReleaseRow).toHaveAttribute('data-status', '已发布')
    await expect(v2ReleaseRow).toContainText(v1ReleaseId)

    const v2ReleaseId = await v2ReleaseRow.getByRole('cell').first().innerText()

    // 回滚 v2 -> 恢复 v1
    await page.getByTestId(`rollback-${v2ReleaseId}`).click()
    await expect(page.getByTestId(`release-row-${v1ReleaseId}`)).toHaveAttribute('data-status', '已发布')
    await expect(page.getByTestId(`release-row-${v2ReleaseId}`)).toHaveAttribute('data-status', '已回滚')
    await expect(page.getByTestId(`detail-row-${v1DetailId}`)).toHaveAttribute('data-status', '已发布')

    // 复算 v1 仍可沿内嵌快照核对（期间曲线已变化两次）
    await page.getByTestId(`recompute-${v1DetailId}`).click()
    await expect(page.locator('body')).toContainText('指纹一致')

    expect(errors).toEqual([])
  })
})
