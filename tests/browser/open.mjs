/** The app opens on the Hub. Older checks work in the learning path, so they open it first. */
export async function openCurriculum(page) {
  await page.locator('[data-view=curriculum]').click();
  await page.locator('#notes').waitFor();
}
