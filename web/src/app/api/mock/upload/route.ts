// Mock upload: accepts the file and returns an id. The mock turn ignores attachments.
export async function POST(request: Request) {
  const form = await request.formData()
  const file = form.get("file")
  if (!(file instanceof File)) return Response.json({ detail: "No file" }, { status: 400 })
  return Response.json({ file_id: `mock_${Date.now().toString(36)}`, name: file.name, size: file.size })
}
