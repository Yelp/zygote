$(document).ready(function () {
	var logged = false;
	var template = null;
	var vars = null;

	var render = function () {
		$('#content').html(Mustache.to_html(template, vars));
	};

	var updateVars = function () {
		$.get('/json', function (data) {
			if (!logged) {
				console.info(data);
				logged = true;
			}
			vars = data;
			if (template) {
				render();
			}
		});
	};

	/* update template */
	$.get('static/template.html', function (data) {
		template = data;
		if (vars) {
			render();
		}
	});

	updateVars();
	setInterval(updateVars, 10000);
});
