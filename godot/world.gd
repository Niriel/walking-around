extends Node3D
## Builds the walkable world at runtime from the pipeline assets:
##   - terrain ArrayMesh from heightmap.f32 (real meters), centered on the origin
##   - a surface-type shader that colors the ground from surface.png (BGT categories)
##   - trimesh collision, a sun + sky, and the first-person player spawned at the center
## Centering on the origin means spawning at (0, ground, 0) lands at the geocoded address.

const ASSETS := "res://assets/"
const PLAYER_SCENE := preload("res://player.tscn")

## Terrain relief here is only ~8 m over 500 m (Groningen is flat). Bump this in the Inspector
## to exaggerate the relief if you want to actually see the hills/dips.
@export var height_scale: float = 1.0

var _n: int                       # grid side length (500)
var _heights: PackedFloat32Array  # row-major, row 0 = north
var _half: float                  # (_n - 1) / 2, the centering offset

func _ready() -> void:
	_load_heightmap()
	var mesh := _build_terrain_mesh()

	var mi := MeshInstance3D.new()
	mi.mesh = mesh	
	mi.material_override = _make_surface_material()
	add_child(mi)

	# Collision via a HeightMapShape3D — the purpose-built heightfield collider. It's centered on
	# the origin with 1-unit cell spacing, the same convention as our centered mesh, so it lines up
	# exactly. (We tried create_trimesh_shape(), but Jolt doesn't collide against a runtime concave
	# trimesh; HeightMapShape3D is the right tool and far lighter.)
	var hm := HeightMapShape3D.new()
	hm.map_width = _n
	hm.map_depth = _n
	var md := PackedFloat32Array()
	md.resize(_n * _n)
	for i in _n * _n:
		md[i] = _heights[i] * height_scale
	hm.map_data = md
	var body := StaticBody3D.new()
	var col := CollisionShape3D.new()
	col.shape = hm
	body.add_child(col)
	add_child(body)

	_add_sun_and_sky()
	_spawn_player()

func _load_heightmap() -> void:
	# Grid size from the sidecar JSON (falls back to 500 if anything's off).
	_n = 500
	var meta_file := FileAccess.open(ASSETS + "heightmap_meta.json", FileAccess.READ)
	if meta_file:
		var meta = JSON.parse_string(meta_file.get_as_text())
		if typeof(meta) == TYPE_DICTIONARY and meta.has("width"):
			_n = int(meta["width"])
	_half = (_n - 1) * 0.5

	# Read _n*_n little-endian float32 meters.
	var f := FileAccess.open(ASSETS + "heightmap.f32", FileAccess.READ)
	assert(f != null, "heightmap.f32 not found in res://assets/")
	_heights = f.get_buffer(_n * _n * 4).to_float32_array()
	assert(_heights.size() == _n * _n, "heightmap.f32 wrong size")

func _h(r: int, c: int) -> float:
	return _heights[r * _n + c] * height_scale

## Grid (row r, col c) -> world position. col->X (west..east), row->Z (north..south, north = -Z).
func _vertex(r: int, c: int) -> Vector3:
	return Vector3(float(c) - _half, _h(r, c), float(r) - _half)

func _build_terrain_mesh() -> ArrayMesh:
	var verts := PackedVector3Array()
	var normals := PackedVector3Array()
	var uvs := PackedVector2Array()
	verts.resize(_n * _n)
	normals.resize(_n * _n)
	uvs.resize(_n * _n)
	var inv := 1.0 / float(_n - 1)

	for r in _n:
		for c in _n:
			var i := r * _n + c
			verts[i] = _vertex(r, c)
			uvs[i] = Vector2(float(c) * inv, float(r) * inv)
			# Analytic normal from height gradients (central differences, edges clamped).
			var hl := _h(r, max(c - 1, 0))
			var hr := _h(r, min(c + 1, _n - 1))
			var hd := _h(max(r - 1, 0), c)
			var hu := _h(min(r + 1, _n - 1), c)
			normals[i] = Vector3(hl - hr, 2.0, hd - hu).normalized()

	# Two triangles per grid cell. Godot's front face is CLOCKWISE (opposite to OpenGL's CCW),
	# so we wind tl->tr->bl / tr->br->bl to make the top surface front-facing under cull_back.
	var indices := PackedInt32Array()
	indices.resize((_n - 1) * (_n - 1) * 6)
	var k := 0
	for r in _n - 1:
		for c in _n - 1:
			var tl := r * _n + c
			var tr := tl + 1
			var bl := tl + _n
			var br := bl + 1
			indices[k] = tl; indices[k + 1] = tr; indices[k + 2] = bl
			indices[k + 3] = tr; indices[k + 4] = br; indices[k + 5] = bl
			k += 6

	var arrays := []
	arrays.resize(Mesh.ARRAY_MAX)
	arrays[Mesh.ARRAY_VERTEX] = verts
	arrays[Mesh.ARRAY_NORMAL] = normals
	arrays[Mesh.ARRAY_TEX_UV] = uvs
	arrays[Mesh.ARRAY_INDEX] = indices

	var mesh := ArrayMesh.new()
	mesh.add_surface_from_arrays(Mesh.PRIMITIVE_TRIANGLES, arrays)
	return mesh

func _make_surface_material() -> ShaderMaterial:
	# Load surface.png via Image (bypasses the texture importer) so category codes stay exact.
	var img := Image.new()
	assert(img.load(ASSETS + "surface.png") == OK, "surface.png failed to load")
	var tex := ImageTexture.create_from_image(img)

	var shader := Shader.new()
	shader.code = """
shader_type spatial;
render_mode cull_back;
uniform sampler2D surface : filter_nearest;
void fragment() {
	int code = int(round(texture(surface, UV).r * 255.0));
	vec3 col = vec3(0.16);                      // 0 unknown (buildings)
	if (code == 1)      col = vec3(0.67, 0.37, 0.27);  // brick
	else if (code == 2) col = vec3(0.78, 0.69, 0.49);  // unpaved / tan
	else if (code == 3) col = vec3(0.37, 0.59, 0.27);  // grass
	else if (code == 4) col = vec3(0.27, 0.43, 0.71);  // water
	else if (code == 5) col = vec3(0.37, 0.37, 0.39);  // asphalt / gray
	ALBEDO = col;
	ROUGHNESS = 0.9;
}
"""
	var mat := ShaderMaterial.new()
	mat.shader = shader
	mat.set_shader_parameter("surface", tex)
	return mat

func _add_sun_and_sky() -> void:
	var sun := DirectionalLight3D.new()
	sun.rotation = Vector3(deg_to_rad(-55.0), deg_to_rad(-130.0), 0.0)  # NW light, like QGIS hillshade
	sun.shadow_enabled = true
	add_child(sun)

	var env := Environment.new()
	env.background_mode = Environment.BG_SKY
	env.sky = Sky.new()
	env.sky.sky_material = ProceduralSkyMaterial.new()
	env.ambient_light_source = Environment.AMBIENT_SOURCE_SKY
	env.ambient_light_energy = 0.4
	var we := WorldEnvironment.new()
	we.environment = env
	add_child(we)

func _spawn_player() -> void:
	var center := _n / 2
	var ground := _h(center, center)
	var player := PLAYER_SCENE.instantiate()
	add_child(player)
	# Drop in just above the ground at the origin (= box center = the geocoded address).
	player.global_position = Vector3(0.0, ground + 1.0, 0.0)
