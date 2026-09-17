# syntax=docker/dockerfile:1

FROM eclipse-temurin:21-jdk AS build

WORKDIR /workspace
COPY gradlew gradlew
COPY gradle gradle
COPY settings.gradle build.gradle gradle.properties ./
# The repository script is checked out with CRLF on Windows; normalize its
# shebang before executing it in the Linux build stage.
RUN sed -i 's/\r$//' gradlew && chmod +x gradlew

# Resolve dependencies in a cache-friendly layer.
RUN ./gradlew dependencies --no-daemon

COPY src src
RUN ./gradlew bootJar --no-daemon

FROM eclipse-temurin:21-jre

WORKDIR /app
RUN useradd --system --create-home --uid 10001 appuser
COPY --from=build /workspace/build/libs/*.jar /app/app.jar
RUN chown -R appuser:appuser /app
USER appuser

ENV JAVA_OPTS="-XX:MaxRAMPercentage=75.0 -Djava.security.egd=file:/dev/./urandom"
EXPOSE 8080
ENTRYPOINT ["sh", "-c", "exec java $JAVA_OPTS -jar /app/app.jar"]
